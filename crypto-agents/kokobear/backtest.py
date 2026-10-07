"""Event-driven backtest of strategy.py with realistic frictions.

Rules that keep it honest:
- signals are computed on a CLOSED candle; fills happen at the NEXT candle's open
- fees + slippage are charged on both sides
- if a candle touches both stop and target, the stop is assumed to fill first
- stops that gap through fill at the (worse) open
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .strategy import StrategyParams, features


@dataclass
class Trade:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry: float
    exit: float
    ret: float          # net fractional return
    bars: int
    reason: str


def run(df: pd.DataFrame, p: StrategyParams, symbol: str = "", cost_per_side: float = 0.002,
        regime: pd.Series | None = None) -> list[Trade]:
    """`regime`: optional boolean series (e.g. BTC uptrend) that must be True to enter."""
    f = features(df, p)
    if regime is not None:
        f["entry"] &= regime.reindex(f.index).ffill().fillna(False).astype(bool)
    o, h, l, c = (f[k].to_numpy() for k in ("open", "high", "low", "close"))
    entry_sig, exit_sig, atr_v = f["entry"].to_numpy(), f["exit"].to_numpy(), f["atr"].to_numpy()
    idx = f.index
    trades: list[Trade] = []
    i, n = 0, len(f)
    while i < n - 1:
        if not entry_sig[i] or np.isnan(atr_v[i]):
            i += 1
            continue
        j = i + 1
        entry = o[j]
        stop = entry - p.stop_atr * atr_v[i]
        target = entry + p.target_atr * atr_v[i] if p.target_atr else np.inf
        exit_px, reason = None, ""
        k = j
        while k < n:
            if o[k] <= stop and k > j:
                exit_px, reason = o[k], "stop-gap"
            elif l[k] <= stop:
                exit_px, reason = stop, "stop"
            elif h[k] >= target:
                exit_px, reason = max(target, o[k]), "target"
            elif exit_sig[k] and k + 1 < n:
                exit_px, reason, k = o[k + 1], "signal", k + 1
            elif k - j + 1 >= p.max_bars and k + 1 < n:
                exit_px, reason, k = o[k + 1], "time", k + 1
            if exit_px is not None:
                break
            k += 1
        if exit_px is None:
            break  # still open at end of data
        ret = (exit_px * (1 - cost_per_side)) / (entry * (1 + cost_per_side)) - 1
        trades.append(Trade(symbol, idx[j], idx[k], entry, exit_px, ret, k - j + 1, reason))
        i = k
    return trades


def summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0}
    r = np.array([t.ret for t in trades])
    wins, losses = r[r > 0], r[r <= 0]
    pf = wins.sum() / -losses.sum() if losses.size and losses.sum() < 0 else float("inf")
    return {
        "trades": len(r),
        "win_rate": float((r > 0).mean()),
        "avg_ret": float(r.mean()),
        "avg_win": float(wins.mean()) if wins.size else 0.0,
        "avg_loss": float(losses.mean()) if losses.size else 0.0,
        "profit_factor": float(pf),
        "sum_ret": float(r.sum()),
        "worst": float(r.min()),
        "avg_bars": float(np.mean([t.bars for t in trades])),
    }


def equity_curve(trades: list[Trade], risk_fraction: float) -> pd.Series:
    """Compounded equity if each trade puts `risk_fraction` of equity at work (sequential)."""
    trades = sorted(trades, key=lambda t: t.exit_time)
    eq, vals, times = 1.0, [], []
    for t in trades:
        eq *= 1 + risk_fraction * t.ret
        vals.append(eq)
        times.append(t.exit_time)
    return pd.Series(vals, index=pd.DatetimeIndex(times), dtype=float)


def max_drawdown(eq: pd.Series) -> float:
    if eq.empty:
        return 0.0
    peak = eq.cummax()
    return float((eq / peak - 1).min())
