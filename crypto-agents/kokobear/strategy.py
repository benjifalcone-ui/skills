"""The technical edge KOKO scans for. Shared by the live agents and the backtester so
what you backtest is exactly what trades.

Setup: "buy the dip inside an uptrend" (short-term mean reversion with a trend filter).
Crypto, like equities, shows short-horizon reversal after sharp drops when the
higher-timeframe trend is still up, plus time-series momentum at longer horizons.
This setup tries to harvest both: only buy when the trend is up, only when the
short-term move is stretched to the downside, and get out as soon as it snaps back.

It is a high-win-rate / small-winner style: most trades win a little, the stop
losses are bigger. Expectancy (avg % per trade after fees) is what matters,
not the win rate on its own.
"""

from dataclasses import dataclass, asdict

import pandas as pd

from .indicators import atr, ema, rsi


@dataclass
class StrategyParams:
    trend_ema: int = 200        # bars; on 4h candles this is ~33 days
    rsi_len: int = 3            # very short RSI = how stretched the last few candles are
    rsi_entry: float = 15.0     # buy when short RSI drops below this...
    rsi_exit: float = 70.0      # ...and sell when it recovers above this
    stop_atr: float = 3.0       # hard stop: entry - stop_atr * ATR
    target_atr: float = 0.0     # optional take-profit in ATRs (0 = off, use rsi_exit)
    max_bars: int = 12          # time stop (12 x 4h = 2 days)
    atr_len: int = 14

    def to_dict(self) -> dict:
        return asdict(self)


def features(df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    out = df.copy()
    out["ema_trend"] = ema(out["close"], p.trend_ema)
    out["rsi"] = rsi(out["close"], p.rsi_len)
    out["atr"] = atr(out, p.atr_len)
    out["uptrend"] = out["close"] > out["ema_trend"]
    out["entry"] = out["uptrend"] & (out["rsi"] < p.rsi_entry)
    out["exit"] = out["rsi"] > p.rsi_exit
    return out


def latest_signal(df: pd.DataFrame, p: StrategyParams) -> dict:
    """Signal on the last closed candle, for the live agents."""
    f = features(df, p).iloc[-1]
    return {
        "time": f.name,
        "close": float(f["close"]),
        "entry": bool(f["entry"]),
        "exit": bool(f["exit"]),
        "uptrend": bool(f["uptrend"]),
        "rsi": float(f["rsi"]),
        "atr": float(f["atr"]),
        "atr_pct": float(f["atr"] / f["close"]),
        "dist_from_trend_pct": float(f["close"] / f["ema_trend"] - 1),
        "stop": float(f["close"] - p.stop_atr * f["atr"]),
        "target": float(f["close"] + p.target_atr * f["atr"]) if p.target_atr else None,
    }
