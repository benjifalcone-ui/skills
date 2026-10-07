"""Persistent account state (positions, equity marks, trade journal) as JSON."""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Position:
    symbol: str
    qty: float              # token units (human, not base units)
    entry_price: float
    cost_usd: float
    stop: float
    entry_time: str
    candle_time: str        # candle that triggered the entry
    bars_held: int = 0
    last_candle: str = ""


@dataclass
class State:
    mode: str
    cash_usd: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    peak_equity: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    halted_reason: str = ""
    journal: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path, mode: str, starting_cash: float) -> "State":
        if path.exists():
            raw = json.loads(path.read_text())
            raw["positions"] = {k: Position(**v) for k, v in raw.get("positions", {}).items()}
            st = cls(**raw)
            if st.mode != mode:
                raise RuntimeError(f"{path} belongs to {st.mode} mode; refusing to mix with {mode}")
            return st
        return cls(mode=mode, cash_usd=starting_cash if mode == "paper" else 0.0)

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        d = asdict(self)
        tmp.write_text(json.dumps(d, indent=2, default=str))
        tmp.replace(path)

    def log(self, event: str, **kw) -> None:
        self.journal.append({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **kw})

    def roll_day(self, equity: float) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if self.day != today:
            self.day, self.day_start_equity = today, equity
        self.peak_equity = max(self.peak_equity, equity)
