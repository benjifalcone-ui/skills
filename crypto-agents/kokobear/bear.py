"""BEAR — the risk manager and trader.

BEAR never invents trades; it only acts on KOKO's BUY / EXIT calls. Its job is deciding
HOW MUCH, and refusing when the account is in trouble.

Position size = the SMALLEST of:
  - quarter-Kelly from the coin's (shrunk) backtest mean / variance of trade returns
  - risk cap: size such that hitting the stop loses at most `risk_per_trade` of equity
  - `max_position` per coin, and what's left under `max_exposure`
then multiplied by KOKO's confidence (news can halve it).

Kill switches: no new buys after a `daily_loss_limit` day, and none at all after a
`max_drawdown_halt` drawdown from the equity peak (clear `halted_reason` in the state file
by hand after you've reviewed what went wrong).
"""

import logging
from datetime import datetime, timezone

from .state import Position

log = logging.getLogger(__name__)


def position_fraction(mean: float, var: float, stop_pct: float, confidence: float, risk) -> float:
    """Fraction of equity to put into one trade. Pure function (unit-tested)."""
    if mean <= 0 or var <= 0 or stop_pct <= 0:
        return 0.0
    kelly = risk.kelly_fraction * mean / var
    risk_cap = risk.risk_per_trade / stop_pct
    return max(0.0, min(kelly, risk_cap, risk.max_position) * confidence)


class Bear:
    def __init__(self, settings, state, broker, calibration):
        self.s, self.state, self.broker, self.cal = settings, state, broker, calibration

    # --- accounting ----------------------------------------------------------------------
    def equity(self, prices: dict[str, float]) -> float:
        held = sum(p.qty * prices.get(sym, p.entry_price) for sym, p in self.state.positions.items())
        return self.broker.usdc_balance() + held

    def _risk_gate(self, equity: float) -> str:
        r = self.s.risk
        if self.state.halted_reason:
            return self.state.halted_reason
        if self.state.peak_equity and equity < self.state.peak_equity * (1 - r.max_drawdown_halt):
            self.state.halted_reason = (f"drawdown halt: equity {equity:.2f} is more than "
                                        f"{r.max_drawdown_halt:.0%} below peak {self.state.peak_equity:.2f}")
            self.state.log("halt", reason=self.state.halted_reason)
            return self.state.halted_reason
        if self.state.day_start_equity and equity < self.state.day_start_equity * (1 - r.daily_loss_limit):
            return f"daily loss limit hit ({equity / self.state.day_start_equity - 1:.1%} today)"
        return ""

    # --- actions -------------------------------------------------------------------------
    def _close(self, sym: str, reason: str) -> None:
        pos = self.state.positions[sym]
        fill = self.broker.sell(sym, pos.qty)
        pnl = fill.usd - pos.cost_usd
        self.state.log("sell", symbol=sym, reason=reason, qty=fill.qty, usd=round(fill.usd, 2),
                       price=fill.price, pnl_usd=round(pnl, 2), pnl_pct=round(pnl / pos.cost_usd, 4),
                       bars=pos.bars_held)
        log.info("BEAR SELL %s (%s) pnl %.2f USD", sym, reason, pnl)
        del self.state.positions[sym]

    def check_stops(self) -> list[str]:
        """Cheap intra-candle check: sell anything trading below its stop."""
        if not self.state.positions:
            return []
        prices = self.broker.prices(list(self.state.positions))
        hit = [s for s, p in self.state.positions.items() if s in prices and prices[s] <= p.stop]
        for sym in hit:
            self._close(sym, f"stop {self.state.positions[sym].stop:.6g} hit at {prices[sym]:.6g}")
        return hit

    def act(self, briefing) -> list[str]:
        """Full cycle on a fresh KOKO briefing. Returns human-readable action lines."""
        did: list[str] = []
        opps = {o.symbol: o for o in briefing.opportunities}
        candle = str(briefing.candle_time)

        # 1) age positions by one candle and run exits
        for sym, pos in list(self.state.positions.items()):
            if pos.last_candle != candle:
                pos.bars_held += 1
                pos.last_candle = candle
            o = opps.get(sym)
            if o and o.action == "EXIT":
                self._close(sym, "exit signal (bounce complete)")
                did.append(f"SELL {sym}: exit signal")
            elif pos.bars_held >= self.s.strategy.max_bars:
                self._close(sym, f"time stop ({pos.bars_held} candles)")
                did.append(f"SELL {sym}: time stop")
        did += [f"SELL {s}: stop" for s in self.check_stops()]

        # 2) risk gate
        symbols = sorted(set(self.s.universe) | set(self.state.positions))
        prices = self.broker.prices(symbols)
        equity = self.equity(prices)
        self.state.roll_day(equity)
        blocked = self._risk_gate(equity)

        # 3) entries
        buys = sorted((o for o in briefing.opportunities if o.action == "BUY"), key=lambda o: -o.score)
        if buys and blocked:
            did.append(f"NO BUYS: {blocked}")
            buys = []
        r = self.s.risk
        for o in buys:
            if o.symbol in self.state.positions:
                continue
            if len(self.state.positions) >= r.max_open_positions:
                did.append(f"skip {o.symbol}: max {r.max_open_positions} open positions")
                break
            st = self.cal["assets"][o.symbol]
            stop_pct = self.s.strategy.stop_atr * o.signal["atr_pct"]
            frac = position_fraction(st["shrunk_mean"], st["shrunk_var"], stop_pct, o.confidence, r)
            exposure = sum(p.qty * prices.get(s, p.entry_price) for s, p in self.state.positions.items())
            frac = min(frac, r.max_exposure - exposure / equity)
            usd = min(equity * frac, self.broker.usdc_balance())
            if usd < r.min_trade_usd:
                did.append(f"skip {o.symbol}: size ${usd:.2f} below minimum")
                continue
            try:
                fill = self.broker.buy(o.symbol, usd)
            except Exception as e:
                did.append(f"skip {o.symbol}: {e}")
                continue
            stop = fill.price * (1 - stop_pct)
            self.state.positions[o.symbol] = Position(
                o.symbol, fill.qty, fill.price, fill.usd, stop,
                datetime.now(timezone.utc).isoformat(timespec="seconds"), candle, 0, candle)
            self.state.log("buy", symbol=o.symbol, usd=round(fill.usd, 2), qty=fill.qty, price=fill.price,
                           stop=stop, pct_of_equity=round(frac, 4), impact_pct=fill.impact_pct,
                           why="; ".join(o.reasons))
            did.append(f"BUY {o.symbol}: ${fill.usd:.2f} ({frac:.1%} of equity) @ {fill.price:.6g}, "
                       f"stop {stop:.6g}")
            log.info("BEAR BUY %s $%.2f", o.symbol, fill.usd)

        self.state.log("mark", equity=round(self.equity(self.broker.prices(symbols)), 2), candle=candle)
        return did
