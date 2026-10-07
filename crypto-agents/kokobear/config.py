"""Settings. Defaults are conservative; override in config.json (see config.example.json).
API keys come ONLY from environment variables, never from files in git."""

import json
from dataclasses import dataclass, field, fields
from pathlib import Path

from .strategy import StrategyParams

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RiskLimits:
    risk_per_trade: float = 0.01        # max account loss if a stop is hit (1%)
    max_position: float = 0.20          # never more than 20% of the account in one coin
    max_exposure: float = 0.60          # never more than 60% of the account in coins at once
    max_open_positions: int = 3
    kelly_fraction: float = 0.25        # quarter-Kelly: full Kelly is far too aggressive for noisy edges
    daily_loss_limit: float = 0.03      # stop opening trades for the day after -3%
    max_drawdown_halt: float = 0.15     # halt all new trades after -15% from the equity peak
    min_trade_usd: float = 10.0
    min_backtest_trades: int = 10       # don't trade a coin with too little history
    shrinkage_trades: int = 30          # pull per-coin stats toward the pooled stats


@dataclass
class Settings:
    mode: str = "paper"                 # paper trading only (see README: live trading)
    interval: str = "4h"
    universe: list[str] = field(default_factory=lambda: ["SOL", "ETH", "BTC", "JUP", "RAY", "JTO", "PYTH", "WIF", "BONK"])
    cost_per_side: float = 0.002        # fees + slippage assumed in backtests
    slippage_bps: int = 50              # Jupiter max slippage on live swaps
    max_price_impact_pct: float = 0.5   # refuse swaps with worse quoted price impact
    paper_starting_usd: float = 1000.0
    use_claude_news: bool = True
    btc_regime_filter: bool = True
    poll_minutes: int = 15              # how often the loop checks stops between candles
    data_dir: Path = ROOT / "data"
    strategy: StrategyParams = field(default_factory=lambda: StrategyParams(
        trend_ema=100, rsi_len=3, rsi_entry=10, rsi_exit=70, stop_atr=3.0, max_bars=12))
    risk: RiskLimits = field(default_factory=RiskLimits)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        s = cls()
        path = path or ROOT / "config.json"
        if path.exists():
            raw = json.loads(path.read_text())
            for f in fields(cls):
                if f.name in raw and f.name not in ("strategy", "risk", "data_dir"):
                    setattr(s, f.name, raw[f.name])
            if "data_dir" in raw:
                s.data_dir = Path(raw["data_dir"])
            for k, v in raw.get("strategy", {}).items():
                setattr(s.strategy, k, v)
            for k, v in raw.get("risk", {}).items():
                setattr(s.risk, k, v)
        if s.mode != "paper":
            raise ValueError("only mode='paper' is implemented; see README 'Going live'")
        s.data_dir.mkdir(parents=True, exist_ok=True)
        return s
