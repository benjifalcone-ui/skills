import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from kokobear import backtest, news
from kokobear.bear import Bear, position_fraction
from kokobear.broker import Fill
from kokobear.config import RiskLimits, Settings
from kokobear.indicators import rsi
from kokobear.koko import Briefing, Opportunity
from kokobear.state import Position, State
from kokobear.strategy import StrategyParams


def ohlc(closes, spread=0.01):
    c = np.asarray(closes, float)
    idx = pd.date_range("2024-01-01", periods=len(c), freq="4h", tz="UTC")
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * (1 + spread), "low": np.minimum(o, c) * (1 - spread),
                         "close": c, "volume": 1.0}, index=idx)


# --- indicators / strategy ------------------------------------------------------------
def test_rsi_bounds_and_direction():
    up = pd.Series(np.linspace(1, 2, 50))
    down = pd.Series(np.linspace(2, 1, 50))
    assert rsi(up, 3).iloc[-1] == 100
    assert rsi(down, 3).iloc[-1] < 1


# --- backtest mechanics ---------------------------------------------------------------
P = StrategyParams(trend_ema=20, rsi_len=2, rsi_entry=10, rsi_exit=70, stop_atr=3.0, max_bars=5)


def test_fills_next_open_and_exits_on_bounce():
    closes = list(np.linspace(100, 200, 40)) + [190, 182, 195, 205, 210]
    df = ohlc(closes, spread=0.001)
    trades = backtest.run(df, P, "X", cost_per_side=0.0)
    assert trades, "dip in an uptrend should trigger"
    t = trades[0]
    entry_bar = df.index.get_loc(t.entry_time)
    assert t.entry == df["open"].iloc[entry_bar]          # filled at the NEXT bar's open
    assert t.reason in ("signal", "time")


def test_stop_beats_target_in_same_bar_and_costs_apply():
    closes = list(np.linspace(100, 200, 40)) + [190, 182, 100, 100, 100, 100, 100]
    df = ohlc(closes)
    t = backtest.run(df, P, "X", cost_per_side=0.002)[0]
    assert t.reason.startswith("stop")
    assert t.ret < 0
    no_cost = backtest.run(df, P, "X", cost_per_side=0.0)[0]
    assert t.ret < no_cost.ret


def test_summarize_and_drawdown():
    ts = [backtest.Trade("X", None, pd.Timestamp("2024-01-0%d" % (i + 1), tz="UTC"), 1, 1, r, 1, "")
          for i, r in enumerate([0.02, -0.05, 0.03])]
    s = backtest.summarize(ts)
    assert s["trades"] == 3 and s["win_rate"] == pytest.approx(2 / 3)
    eq = backtest.equity_curve(ts, 1.0)
    assert backtest.max_drawdown(eq) == pytest.approx(-0.05)


# --- BEAR sizing ------------------------------------------------------------------------
def test_position_fraction_takes_the_smallest_cap():
    r = RiskLimits()
    # big Kelly, 5% stop -> risk cap 1%/5% = 20% == max_position
    assert position_fraction(0.01, 0.002, 0.05, 1.0, r) == pytest.approx(0.20)
    # 10% stop -> risk cap 10%
    assert position_fraction(0.01, 0.002, 0.10, 1.0, r) == pytest.approx(0.10)
    # tiny edge -> quarter-Kelly binds: 0.25*0.0001/0.002 = 1.25%
    assert position_fraction(0.0001, 0.002, 0.05, 1.0, r) == pytest.approx(0.0125)
    # news halves it, no edge means no trade
    assert position_fraction(0.01, 0.002, 0.10, 0.5, r) == pytest.approx(0.05)
    assert position_fraction(-0.01, 0.002, 0.05, 1.0, r) == 0


class FakeBroker:
    def __init__(self, state, px):
        self.state, self.px = state, px

    def prices(self, symbols):
        return {s: self.px[s] for s in symbols if s in self.px}

    def usdc_balance(self):
        return self.state.cash_usd

    def buy(self, sym, usd):
        self.state.cash_usd -= usd
        return Fill(sym, "buy", usd / self.px[sym], usd, self.px[sym])

    def sell(self, sym, qty):
        self.state.cash_usd += qty * self.px[sym]
        return Fill(sym, "sell", qty, qty * self.px[sym], self.px[sym])


CAL = {"assets": {s: {"shrunk_mean": 0.01, "shrunk_var": 0.002, "enabled": True} for s in ("SOL", "JUP", "RAY", "ETH")}}


def opp(sym, action, atr_pct=0.02, conf=1.0):
    sig = {"close": 1.0, "rsi": 5, "atr_pct": atr_pct, "dist_from_trend_pct": 0.01, "entry": action == "BUY",
           "exit": action == "EXIT", "uptrend": True}
    return Opportunity(sym, action, sig, {"score": 0}, 0.01, 0.65, conf, True, ["test"])


def make(tmp_path, cash=1000.0, **px):
    s = Settings.load(tmp_path / "none.json")
    s.data_dir = tmp_path
    st = State("paper", cash_usd=cash)
    return s, st, FakeBroker(st, {"SOL": 100.0, "JUP": 1.0, "RAY": 2.0, "ETH": 2000.0, **px})


def test_bear_buys_ranked_and_respects_limits(tmp_path):
    s, st, br = make(tmp_path)
    b = Briefing("c1", True, None, {}, [opp(x, "BUY") for x in ("SOL", "JUP", "RAY", "ETH")], 0)
    actions = Bear(s, st, br, CAL).act(b)
    assert len(st.positions) == s.risk.max_open_positions == 3
    # 3% ATR stop distance * 2% atr -> 6% stop -> 1%/6% = 16.7% each
    assert st.positions["SOL"].cost_usd == pytest.approx(1000 * 0.01 / 0.06, rel=1e-3)
    assert st.positions["SOL"].stop == pytest.approx(100 * (1 - 0.06))
    assert any("max 3 open positions" in a for a in actions)
    exposure = sum(p.cost_usd for p in st.positions.values())
    assert exposure <= 1000 * s.risk.max_exposure + 1e-9


def test_bear_exits_on_signal_stop_and_time(tmp_path):
    s, st, br = make(tmp_path)
    st.positions = {
        "SOL": Position("SOL", 1, 100, 100, 90, "t", "c0", 0, "c0"),
        "JUP": Position("JUP", 100, 1.0, 100, 0.95, "t", "c0", 0, "c0"),
        "RAY": Position("RAY", 50, 2.0, 100, 1.5, "t", "c0", s.strategy.max_bars - 1, "c0"),
    }
    st.cash_usd = 700
    br.px["JUP"] = 0.90  # below stop
    b = Briefing("c1", True, None, {}, [opp("SOL", "EXIT"), opp("JUP", "NONE"), opp("RAY", "NONE")], 0)
    acts = Bear(s, st, br, CAL).act(b)
    assert st.positions == {}
    assert any("SOL: exit signal" in a for a in acts)
    assert any("RAY: time stop" in a for a in acts)
    assert any("JUP: stop" in a for a in acts)


def test_bear_kill_switches(tmp_path):
    s, st, br = make(tmp_path, cash=800)
    st.peak_equity = 1000  # -20% from peak > 15% halt
    b = Briefing("c1", True, None, {}, [opp("SOL", "BUY")], 0)
    acts = Bear(s, st, br, CAL).act(b)
    assert not st.positions and st.halted_reason and any("NO BUYS" in a for a in acts)

    s, st, br = make(tmp_path, cash=960)
    st.day = datetime.now(timezone.utc).date().isoformat()
    st.day_start_equity, st.peak_equity = 1000, 1000  # -4% today > 3% limit
    acts = Bear(s, st, br, CAL).act(b)
    assert not st.positions and any("daily loss" in a for a in acts)


# --- news / state / config ------------------------------------------------------------
def test_keyword_scores_flags_only_named_coin():
    hs = [news.Headline("x", "Solana DEX drained in $40M exploit", "", None, ""),
          news.Headline("x", "Bitcoin ETF inflows surge to record", "", None, "")]
    sc = news.keyword_scores(hs)
    assert sc["SOL"]["severe"] and sc["SOL"]["score"] < 0
    assert not sc["BTC"]["severe"] and sc["BTC"]["score"] > 0
    assert not sc["ETH"]["severe"] and sc["ETH"]["n"] == 0


def test_state_roundtrip_and_mode_guard(tmp_path):
    p = tmp_path / "s.json"
    st = State.load(p, "paper", 500)
    st.positions["SOL"] = Position("SOL", 1, 100, 100, 90, "t", "c")
    st.log("buy", symbol="SOL")
    st.save(p)
    st2 = State.load(p, "paper", 0)
    assert st2.cash_usd == 500 and st2.positions["SOL"].stop == 90 and st2.journal[0]["event"] == "buy"
    with pytest.raises(RuntimeError):
        State.load(p, "live", 0)


def test_config_rejects_live_mode(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mode": "live", "data_dir": str(tmp_path)}))
    with pytest.raises(ValueError):
        Settings.load(cfg)
