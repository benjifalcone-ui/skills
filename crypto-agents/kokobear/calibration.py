"""Turn full-history backtests into the per-coin stats BEAR sizes with.

Per-coin numbers are noisy (a coin may have only 10-60 trades), so each coin's mean and
variance are shrunk toward the pooled numbers across all coins before use."""

import json
from datetime import datetime, timedelta, timezone

import numpy as np

from . import backtest, data
from .assets import ASSETS
from .strategy import features


def run(settings, days: int = 2000) -> dict:
    cache = settings.data_dir / "cache"
    p = settings.strategy
    dfs = {s: data.history(ASSETS[s].binance_pair, settings.interval, days, cache) for s in settings.universe}
    if "BTC" not in dfs:
        dfs_btc = data.history(ASSETS["BTC"].binance_pair, settings.interval, days, cache)
    else:
        dfs_btc = dfs["BTC"]
    regime = features(dfs_btc, p)["uptrend"] if settings.btc_regime_filter else None

    per, pooled = {}, []
    for sym, df in dfs.items():
        trades = backtest.run(df, p, sym, settings.cost_per_side, regime)
        rets = np.array([t.ret for t in trades])
        pooled.extend(rets.tolist())
        per[sym] = {"n": len(rets), **backtest.summarize(trades),
                    "mean": float(rets.mean()) if len(rets) else 0.0,
                    "var": float(rets.var()) if len(rets) > 1 else 0.0}
    pooled_arr = np.array(pooled)
    pooled_stats = {"n": len(pooled_arr), "mean": float(pooled_arr.mean()), "var": float(pooled_arr.var()),
                    "win_rate": float((pooled_arr > 0).mean())}

    k = settings.risk.shrinkage_trades
    for sym, st in per.items():
        n = st["n"]
        st["shrunk_mean"] = (n * st["mean"] + k * pooled_stats["mean"]) / (n + k)
        st["shrunk_var"] = (n * st["var"] + k * pooled_stats["var"]) / (n + k)
        st["shrunk_win_rate"] = (n * st.get("win_rate", 0) + k * pooled_stats["win_rate"]) / (n + k)
        st["enabled"] = bool(n >= settings.risk.min_backtest_trades and st["shrunk_mean"] > 0
                             and st.get("avg_ret", 0) > 0)

    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "strategy": p.to_dict(), "cost_per_side": settings.cost_per_side,
           "pooled": pooled_stats, "assets": per}
    (settings.data_dir / "calibration.json").write_text(json.dumps(out, indent=2))
    return out


def load(settings, max_age_days: int = 7) -> dict:
    path = settings.data_dir / "calibration.json"
    if not path.exists():
        return run(settings)
    cal = json.loads(path.read_text())
    age = datetime.now(timezone.utc) - datetime.fromisoformat(cal["generated"])
    if (cal.get("strategy") != settings.strategy.to_dict() or cal.get("cost_per_side") != settings.cost_per_side
            or age > timedelta(days=max_age_days)):
        return run(settings)  # strategy changed or stats are stale
    return cal
