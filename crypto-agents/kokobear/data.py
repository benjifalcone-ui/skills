"""Free, keyless market data sources.

- Binance public market-data mirror (data-api.binance.vision): OHLCV candles
- alternative.me: Crypto Fear & Greed Index
- CoinGecko: 24h volume / market-cap snapshot
- Jupiter: live on-chain quotes and prices for the Solana tokens BEAR trades
"""

import time
from pathlib import Path

import pandas as pd
import requests

BINANCE = "https://data-api.binance.vision/api/v3/klines"
FNG = "https://api.alternative.me/fng/"
COINGECKO = "https://api.coingecko.com/api/v3/coins/markets"
JUP_PRICE = "https://lite-api.jup.ag/price/v3"

INTERVAL_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
_session = requests.Session()
_session.headers["User-Agent"] = "kokobear/0.1"


def _get(url: str, params: dict | None = None, timeout: int = 20, retries: int = 3):
    for attempt in range(retries):
        try:
            r = _session.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def klines(pair: str, interval: str = "4h", limit: int = 500, start_ms: int | None = None) -> pd.DataFrame:
    """Most recent `limit` candles (or from `start_ms`). Drops the still-open candle."""
    params = {"symbol": pair, "interval": interval, "limit": min(limit, 1000)}
    if start_ms is not None:
        params["startTime"] = start_ms
    rows = _get(BINANCE, params)
    df = pd.DataFrame(
        rows,
        columns=["open_time", "open", "high", "low", "close", "volume", "close_time",
                 "qv", "n", "tbv", "tqv", "_"],
    )[["open_time", "open", "high", "low", "close", "volume", "close_time"]]
    df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
    df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    now_ms = int(time.time() * 1000)
    df = df[df["close_time"] < now_ms]
    return df.set_index("time")[["open", "high", "low", "close", "volume"]]


def history(pair: str, interval: str = "4h", days: int = 1500, cache_dir: Path | None = None) -> pd.DataFrame:
    """Full paginated history, optionally cached to CSV (refreshes the tail on each call)."""
    cache = cache_dir / f"{pair}_{interval}.csv" if cache_dir else None
    step = INTERVAL_MS[interval]
    start = int(time.time() * 1000) - days * 86_400_000
    frames = []
    if cache and cache.exists():
        old = pd.read_csv(cache, index_col="time", parse_dates=["time"])
        frames.append(old)
        start = int(old.index[-1].timestamp() * 1000) + step
    while True:
        chunk = klines(pair, interval, 1000, start_ms=start)
        if chunk.empty:
            break
        frames.append(chunk)
        start = int(chunk.index[-1].timestamp() * 1000) + step
        if len(chunk) < 999:
            break
    df = pd.concat(frames) if frames else pd.DataFrame()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if cache is not None and not df.empty:
        cache.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache, index_label="time")
    return df


def fear_greed(limit: int = 1) -> list[dict]:
    """[{'value': 71, 'label': 'Greed', 'time': Timestamp}, ...] newest first."""
    data = _get(FNG, {"limit": limit})["data"]
    return [
        {"value": int(d["value"]), "label": d["value_classification"],
         "time": pd.to_datetime(int(d["timestamp"]), unit="s", utc=True)}
        for d in data
    ]


def coingecko_snapshot(ids: list[str]) -> dict[str, dict]:
    rows = _get(COINGECKO, {"vs_currency": "usd", "ids": ",".join(ids),
                            "price_change_percentage": "24h,7d"})
    return {r["id"]: r for r in rows}


def jupiter_prices(mints: list[str]) -> dict[str, float]:
    data = _get(JUP_PRICE, {"ids": ",".join(mints)})
    return {m: float(v["usdPrice"]) for m, v in data.items() if v and "usdPrice" in v}
