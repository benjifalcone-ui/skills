"""Paper execution priced off REAL Jupiter quotes, so simulated fills include true
on-chain price impact for the size you trade. No keys, no signing, nothing is sent."""

import logging
from dataclasses import dataclass

import requests

from .assets import ASSETS, USDC_DECIMALS, USDC_MINT
from .data import jupiter_prices

log = logging.getLogger(__name__)

JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"


@dataclass
class Fill:
    symbol: str
    side: str
    qty: float          # token units
    usd: float          # USDC spent (buy) or received (sell)
    price: float
    impact_pct: float = 0.0


class SwapRejected(Exception):
    pass


def jupiter_quote(input_mint: str, output_mint: str, amount_base: int, slippage_bps: int) -> dict:
    r = requests.get(JUP_QUOTE, params={"inputMint": input_mint, "outputMint": output_mint,
                                        "amount": amount_base, "slippageBps": slippage_bps}, timeout=20)
    r.raise_for_status()
    q = r.json()
    if "outAmount" not in q:
        raise SwapRejected(f"no route: {q}")
    return q


class PaperBroker:
    def __init__(self, settings, state):
        self.s, self.state = settings, state

    def prices(self, symbols: list[str]) -> dict[str, float]:
        px = jupiter_prices([ASSETS[s].mint for s in symbols])
        return {s: px[ASSETS[s].mint] for s in symbols if ASSETS[s].mint in px}

    def usdc_balance(self) -> float:
        return self.state.cash_usd

    def _quote(self, symbol: str, side: str, amount: float) -> tuple[float, float]:
        a = ASSETS[symbol]
        if side == "buy":
            q = jupiter_quote(USDC_MINT, a.mint, int(amount * 10 ** USDC_DECIMALS), self.s.slippage_bps)
            out = int(q["outAmount"]) / 10 ** a.decimals
        else:
            q = jupiter_quote(a.mint, USDC_MINT, int(amount * 10 ** a.decimals), self.s.slippage_bps)
            out = int(q["outAmount"]) / 10 ** USDC_DECIMALS
        impact = abs(float(q.get("priceImpactPct") or 0)) * 100
        if impact > self.s.max_price_impact_pct:
            raise SwapRejected(f"{symbol} price impact {impact:.2f}% > limit")
        return out, impact

    def buy(self, symbol: str, usd: float) -> Fill:
        qty, impact = self._quote(symbol, "buy", usd)
        self.state.cash_usd -= usd
        return Fill(symbol, "buy", qty, usd, usd / qty, impact)

    def sell(self, symbol: str, qty: float) -> Fill:
        usd, impact = self._quote(symbol, "sell", qty)
        self.state.cash_usd += usd
        return Fill(symbol, "sell", qty, usd, usd / qty, impact)
