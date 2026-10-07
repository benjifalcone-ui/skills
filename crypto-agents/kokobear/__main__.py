"""CLI:  python -m kokobear {calibrate|backtest|brief|run|loop|status}"""

import argparse
import json
import logging
import time
from datetime import datetime, timezone

import pandas as pd

from . import backtest, calibration, data
from .assets import ASSETS
from .bear import Bear
from .broker import PaperBroker
from .config import Settings
from .koko import Koko
from .state import State
from .strategy import features

INTERVAL_S = {"1h": 3600, "4h": 14400, "1d": 86400}


def _state(s: Settings) -> tuple[State, object]:
    path = s.data_dir / f"state_{s.mode}.json"
    st = State.load(path, s.mode, s.paper_starting_usd)
    return st, path


def cmd_calibrate(s: Settings, _):
    cal = calibration.run(s)
    rows = [{"coin": k, "trades": v["n"], "win_rate": v.get("win_rate", 0), "avg/trade": v.get("avg_ret", 0),
             "profit_factor": v.get("profit_factor", 0), "enabled": v["enabled"]} for k, v in cal["assets"].items()]
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("pooled:", {k: round(v, 4) for k, v in cal["pooled"].items()})


def cmd_backtest(s: Settings, a):
    cache = s.data_dir / "cache"
    p = s.strategy
    dfs = {sym: data.history(ASSETS[sym].binance_pair, s.interval, a.days, cache) for sym in s.universe}
    btc = dfs.get("BTC")
    if btc is None:
        btc = data.history("BTCUSDT", s.interval, a.days, cache)
    regime = features(btc, p)["uptrend"] if s.btc_regime_filter else None
    trades = [t for sym, df in dfs.items() for t in backtest.run(df, p, sym, s.cost_per_side, regime)]
    split = pd.Timestamp(a.split, tz="UTC")
    for name, ts in (("in-sample  (< %s)" % a.split, [t for t in trades if t.entry_time < split]),
                     ("out-of-sample (>= %s)" % a.split, [t for t in trades if t.entry_time >= split]),
                     ("all", trades)):
        sm = backtest.summarize(ts)
        print(f"{name:28s}", {k: round(v, 4) if isinstance(v, float) else v for k, v in sm.items()})
    eq = backtest.equity_curve(trades, a.fraction)
    if not eq.empty:
        print(f"\ncompounded at {a.fraction:.0%} of equity per trade: {eq.iloc[-1] - 1:+.1%} total, "
              f"max drawdown {backtest.max_drawdown(eq):.1%}")
    t = pd.DataFrame([vars(x) for x in trades])
    if not t.empty:
        t["year"] = t.entry_time.dt.year
        print("\nby year:\n", t.groupby("year")["ret"].agg(trades="size", win_rate=lambda r: (r > 0).mean(),
                                                           avg="mean").round(4))


def cmd_brief(s: Settings, _):
    b = Koko(s, calibration.load(s)).scan()
    md = b.to_markdown()
    out = s.data_dir / "reports"
    out.mkdir(exist_ok=True)
    path = out / f"brief_{datetime.now(timezone.utc):%Y-%m-%d_%H%M}.md"
    path.write_text(md)
    print(md)
    print(f"\n(saved to {path})")
    return b


def cmd_run(s: Settings, _):
    cal = calibration.load(s)
    st, path = _state(s)
    b = Koko(s, cal).scan()
    print(b.to_markdown())
    actions = Bear(s, st, PaperBroker(s, st), cal).act(b)
    st.save(path)
    print("\nBEAR:", *(actions or ["no trades"]), sep="\n  ")


def cmd_loop(s: Settings, _):
    """Run forever: full KOKO+BEAR cycle right after each candle closes, stop checks in between."""
    cal = calibration.load(s)
    st, path = _state(s)
    broker = PaperBroker(s, st)
    bear = Bear(s, st, broker, cal)
    period = INTERVAL_S[s.interval]
    last_cycle = None
    logging.info("loop started (%s mode, %s candles)", s.mode, s.interval)
    while True:
        try:
            now = time.time()
            candle = int(now // period)
            if candle != last_cycle and now - candle * period > 60:  # give the exchange a minute
                cal = bear.cal = calibration.load(s)  # re-runs backtests when a week old
                b = Koko(s, cal).scan()
                for line in bear.act(b) or ["no trades"]:
                    logging.info("BEAR %s", line)
                last_cycle = candle
            else:
                bear.check_stops()
            st.save(path)
        except Exception:
            logging.exception("cycle failed; will retry")
        time.sleep(s.poll_minutes * 60)


def cmd_status(s: Settings, a):
    st, _ = _state(s)
    broker = PaperBroker(s, st)
    prices = broker.prices(sorted(set(st.positions) | {"SOL"}))
    eq = Bear(s, st, broker, {"assets": {}}).equity(prices)
    print(f"mode={s.mode} equity=${eq:,.2f} cash=${broker.usdc_balance():,.2f} peak=${st.peak_equity:,.2f}"
          + (f"  HALTED: {st.halted_reason}" if st.halted_reason else ""))
    for sym, p in st.positions.items():
        px = prices.get(sym, p.entry_price)
        print(f"  {sym}: {p.qty:.6g} @ {p.entry_price:.6g} now {px:.6g} ({px / p.entry_price - 1:+.2%}) "
              f"stop {p.stop:.6g} bars {p.bars_held}")
    closed = [j for j in st.journal if j["event"] == "sell"]
    if closed:
        wins = sum(j["pnl_usd"] > 0 for j in closed)
        print(f"closed trades: {len(closed)}  win rate {wins / len(closed):.0%}  "
              f"total P&L ${sum(j['pnl_usd'] for j in closed):,.2f}")
    for j in st.journal[-a.tail:]:
        print("  ", json.dumps(j))


def main():
    ap = argparse.ArgumentParser(prog="kokobear")
    ap.add_argument("--config", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("calibrate", help="backtest every coin and store the stats BEAR sizes with")
    bt = sub.add_parser("backtest", help="in-sample vs out-of-sample backtest")
    bt.add_argument("--days", type=int, default=2000)
    bt.add_argument("--split", default="2025-01-01")
    bt.add_argument("--fraction", type=float, default=0.10, help="equity fraction per trade for the curve")
    sub.add_parser("brief", help="KOKO's briefing: what to do on each coin right now")
    sub.add_parser("run", help="one KOKO + BEAR cycle (paper)")
    sub.add_parser("loop", help="run continuously (paper)")
    stp = sub.add_parser("status", help="account, positions, recent journal")
    stp.add_argument("--tail", type=int, default=10)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose or a.cmd == "loop" else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from pathlib import Path
    s = Settings.load(Path(a.config) if a.config else None)
    {"calibrate": cmd_calibrate, "backtest": cmd_backtest, "brief": cmd_brief, "run": cmd_run,
     "loop": cmd_loop, "status": cmd_status}[a.cmd](s, a)


if __name__ == "__main__":
    main()
