"""KOKO — the research agent.

Every candle close KOKO gathers, per coin:
  1. technicals: trend, short-term RSI stretch, volatility (ATR), entry/exit signal
  2. market regime: is BTC itself in an uptrend? (dips in a falling market keep falling)
  3. news: last-24h headlines from major crypto outlets, scored per coin (Claude or keywords)
  4. sentiment: Crypto Fear & Greed index (reported for context; not used in sizing because
     it did not survive testing as a filter)
  5. track record: how this exact setup has performed on this coin historically
and turns it into a ranked list of opportunities with a plain-English reason for each.
"""

import logging
from dataclasses import dataclass, field

from . import data, news
from .assets import ASSETS
from .strategy import latest_signal

log = logging.getLogger(__name__)


@dataclass
class Opportunity:
    symbol: str
    action: str                     # BUY | EXIT | WATCH | AVOID | NONE
    signal: dict
    news: dict
    expected_ret: float             # historical avg net return per trade (shrunk)
    win_rate: float
    confidence: float               # 0..1 multiplier BEAR applies to size
    enabled: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.expected_ret * self.confidence if self.action == "BUY" else 0.0


@dataclass
class Briefing:
    candle_time: object
    btc_uptrend: bool
    fear_greed: dict | None
    market_news: dict
    opportunities: list[Opportunity]
    headlines: int

    def ranked(self) -> list[Opportunity]:
        order = {"BUY": 0, "EXIT": 1, "WATCH": 2, "AVOID": 3, "NONE": 4}
        return sorted(self.opportunities, key=lambda o: (order[o.action], -o.score, o.symbol))

    def to_markdown(self) -> str:
        fg = self.fear_greed
        lines = [
            f"# KOKO briefing — last closed candle opened {self.candle_time}",
            "",
            f"- BTC regime: **{'UPTREND (dip-buying allowed)' if self.btc_uptrend else 'DOWNTREND (no new buys)'}**",
            f"- Fear & Greed: **{fg['value']} ({fg['label']})**" if fg else "- Fear & Greed: unavailable",
            f"- Market news tone: {self.market_news.get('score', 0):+.2f} from {self.headlines} headlines",
            "",
            "| Coin | Action | Price | RSI | vs trend | Hist. win | Hist. avg/trade | News | Why |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for o in self.ranked():
            s = o.signal
            lines.append(
                f"| {o.symbol} | **{o.action}** | {s['close']:.6g} | {s['rsi']:.0f} | "
                f"{s['dist_from_trend_pct']:+.1%} | {o.win_rate:.0%} | {o.expected_ret:+.2%} | "
                f"{o.news.get('score', 0):+.2f}{' ⚠' if o.news.get('severe') else ''} | {'; '.join(o.reasons)} |"
            )
        lines += ["", "_Historical stats are backtests net of 0.2%/side costs. Past performance does not "
                      "guarantee future results._"]
        return "\n".join(lines)


class Koko:
    def __init__(self, settings, calibration: dict):
        self.s, self.cal = settings, calibration

    def scan(self) -> Briefing:
        p = self.s.strategy
        bars = p.trend_ema * 3 + 50
        btc_sig = latest_signal(data.klines(ASSETS["BTC"].binance_pair, self.s.interval, bars), p)
        btc_up = btc_sig["uptrend"] or not self.s.btc_regime_filter

        try:
            fg = data.fear_greed(1)[0]
        except Exception as e:
            log.warning("fear & greed unavailable: %s", e)
            fg = None

        try:
            scores, headlines = news.sentiment(24, self.s.use_claude_news)
        except Exception as e:
            log.warning("news unavailable: %s", e)
            scores, headlines = {}, []
        market = scores.get("MARKET", {"score": 0.0})

        opps = []
        for sym in self.s.universe:
            sig = btc_sig if sym == "BTC" else latest_signal(
                data.klines(ASSETS[sym].binance_pair, self.s.interval, bars), p)
            opps.append(self._assess(sym, sig, scores.get(sym, {"score": 0.0, "severe": False}), market, btc_up))
        return Briefing(btc_sig["time"], btc_up, fg, market, opps, len(headlines))

    def _assess(self, sym, sig, nws, market, btc_up) -> Opportunity:
        st = self.cal["assets"].get(sym, {})
        o = Opportunity(sym, "NONE", sig, nws, st.get("shrunk_mean", 0.0), st.get("shrunk_win_rate", 0.0),
                        1.0, st.get("enabled", False))
        if sig["exit"]:
            o.action = "EXIT"
            o.reasons.append(f"RSI({self.s.strategy.rsi_len}) {sig['rsi']:.0f} — bounce done, take profit if held")
            return o
        if sig["entry"]:
            if not o.enabled:
                o.action = "AVOID"
                o.reasons.append("setup fired but this coin's backtest edge is not positive")
            elif not btc_up:
                o.action = "AVOID"
                o.reasons.append("dip signal, but BTC is below its trend")
            elif nws.get("severe"):
                o.action = "AVOID"
                o.reasons.append("dip coincides with severe news: " + "; ".join(nws.get("top", [])[:1] or
                                                                                [nws.get("reason", "")]))
            else:
                o.action = "BUY"
                o.reasons.append(f"uptrend dip: RSI({self.s.strategy.rsi_len}) {sig['rsi']:.0f}, "
                                 f"{sig['dist_from_trend_pct']:+.1%} vs trend")
                if nws.get("score", 0) < -0.5 or market.get("score", 0) < -0.5:
                    o.confidence = 0.5
                    o.reasons.append("negative news tone → half size")
            return o
        if sig["uptrend"] and sig["rsi"] < 30:
            o.action = "WATCH"
            o.reasons.append("in uptrend and dipping; not stretched enough yet")
        elif not sig["uptrend"]:
            o.reasons.append("below trend")
        else:
            o.reasons.append("no setup")
        return o
