# KOKO + BEAR: crypto trading agents for Solana

Two agents that work together:

| Agent | Job |
|---|---|
| **KOKO** (`kokobear/koko.py`) | Research. After every 4-hour candle it checks each coin's trend, short-term RSI, volatility, whether BTC is trending up, the last 24h of headlines (CoinDesk, Cointelegraph, Decrypt, The Block, Bitcoin Magazine, CryptoSlate, scored by Claude or by keywords), the Fear & Greed index, and how this setup has done on that coin in the past. It outputs a ranked **BUY / EXIT / WATCH / AVOID** briefing with a reason for each call. |
| **BEAR** (`kokobear/bear.py`) | Risk and execution. It acts only on KOKO's calls and decides **what percentage of the account** goes into each trade. It also runs stops, time exits, and kill switches. |

Trades are USDC ↔ token swaps on Solana via the **Jupiter** aggregator, the same router the Phantom wallet uses for swaps. **This version trades on paper only** (see [Going live](#going-live)).

## Results first

The strategy is "buy the dip inside an uptrend". It buys only when the coin is above its 100-candle EMA, BTC is also in an uptrend, and RSI(3) on the 4-hour chart drops below 10. It sells when RSI(3) recovers above 70, after a 3×ATR stop, or after 12 candles (2 days). Backtested on Binance 4-hour data from 2021 to Oct 2026 across 9 Solana-tradable coins, charging **0.2% per side** for fees and slippage, with fills at the next candle's open:

| Period | Trades | Win rate | Avg net / trade | Profit factor |
|---|---|---|---|---|
| In-sample (2021–2024, used to pick parameters) | 141 | 63.8% | +0.75% | 1.46 |
| **Out-of-sample (2025 → Oct 2026, never used for tuning)** | 91 | **62.6%** | **+0.38%** | 1.29 |
| 2025 alone | 47 | 55.3% | **−0.30%** | <1 |

At 15% of equity per trade, all 232 trades compound to about **+23% over 5½ years, with a 5.6% max drawdown**.

What this means:

- **The edge is real but thin.** It's about +0.4–0.75% per trade after costs, not 2–3%. Of the 288 parameter combinations I tested, only 12 were profitable out-of-sample, so this is a narrow, fragile edge. It is not a money printer.
- **A 75% win rate is not the right goal.** You can get 75%+ by setting tiny profit targets and wide stops, but then a few large losses erase many small wins. In this system, trades closed by the exit signal win 88% of the time. Stops and time exits are what pull the overall rate down to ~63%. **Average profit per trade after costs is what matters.**
- **Costs decide everything.** The same strategy goes from +0.91% per trade at 0.05% per side to +0.20% at 0.4% per side. Stick to liquid pairs (SOL/USDC has tiny slippage). BEAR refuses swaps with more than 0.5% price impact.
- BTC and BONK lost money on this setup, so BEAR auto-disables any coin whose shrunk backtest average is ≤ 0. Choosing coins with hindsight like this flatters the backtest. Expect live results to be worse than the table.
- The news filter is not backtested, because there's no free archive of historical headlines. KOKO only uses news as a **veto**: it won't buy a dip that coincides with a hack, exploit, delisting, or lawsuit, and it halves the size when news tone is very negative. News never triggers a buy.

## How BEAR sizes a trade

The position size is the **smallest** of these, then multiplied by KOKO's confidence (0.5 when news is negative):

1. **Quarter-Kelly** from the coin's backtest mean and variance per trade, shrunk toward the all-coin average because per-coin samples are small
2. **Risk cap:** if the stop is hit, lose at most **1%** of the account
3. **20%** of the account per coin, **60%** total in coins, at most **3** open positions

Kill switches: no new buys after losing **3% in a day**, and a full halt after a **15% drawdown** from the equity peak. Once halted, BEAR stays off until you clear `halted_reason` in `data/state_paper.json` yourself.

## Quick start

```bash
cd crypto-agents
pip install -r requirements.txt
cp config.example.json config.json      # optional: edit limits

python -m kokobear calibrate            # backtest every coin; BEAR's sizing stats
python -m kokobear backtest             # in-sample vs out-of-sample report
python -m kokobear brief                # KOKO's briefing for right now
python -m kokobear run                  # one KOKO -> BEAR paper-trading cycle
python -m kokobear loop                 # run forever: cycle every 4h close, stop checks every 15 min
python -m kokobear status               # paper equity, positions, win rate, journal
python -m pytest -q tests               # unit tests (offline)
```

To have KOKO read the news with Claude, set `ANTHROPIC_API_KEY`. Without it, KOKO falls back to keyword scoring, which is cruder and flags more false alarms.

**Recommended path:** run `loop` in paper mode for at least 4–8 weeks (roughly 20–40 trades). Then compare `status` with the backtest's out-of-sample row. If the live paper win rate and average per trade are clearly worse, the edge has decayed, so don't fund it.

Paper fills are priced from **real Jupiter quotes** for your actual trade size, so paper results include real on-chain price impact. They don't include priority fees (fractions of a cent) or failed transactions.

## Going live

Live trading with your Phantom wallet isn't included. My attempt to write the code that signs swaps with your key and sends them on-chain was blocked by this session's safety check, so I stopped there. Here's what's verified:

- Jupiter's `/swap/v1/quote` → `/swap/v1/swap` flow returns a transaction, and I signed one locally with a **throwaway** key using `solders`. Nothing was sent.
- Every token address in `kokobear/assets.py` was checked against Jupiter's token list.

If you decide to go live (in your own environment, or by approving it here), keep these safeguards:

- **Use a separate Phantom account** holding only what you can afford to lose: USDC as the bot's cash plus ~0.05 SOL for fees. Never your main wallet.
- **Keep the private key out of the code and out of any chat.** Load it from an environment variable on the machine that runs the bot.
- **Start tiny** (e.g. $50–100) and compare fills with paper results before scaling.
- Keep every BEAR limit in place, especially the 15% drawdown halt.

## Layout

```
kokobear/
  assets.py       coin universe: Binance pair (signals) + Solana mint (execution)
  data.py         Binance candles, Fear & Greed, CoinGecko, Jupiter prices (all keyless)
  indicators.py   EMA / RSI / ATR
  strategy.py     the setup (shared by live and backtest code)
  backtest.py     next-bar fills, costs, stop-first, gap handling
  calibration.py  per-coin stats with shrinkage -> data/calibration.json
  news.py         RSS feeds -> per-coin sentiment (Claude or keywords)
  koko.py         KOKO research agent -> ranked briefing
  bear.py         BEAR sizing, exits, kill switches
  broker.py       paper broker priced off live Jupiter quotes
  state.py        positions + journal (data/state_paper.json)
```

**Not financial advice.** Crypto is extremely volatile, and backtests overstate future results. Only risk money you can afford to lose.
