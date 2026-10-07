"""Tradable universe: each asset has a Binance pair (deep price history for signals)
and a Solana mint (where BEAR actually executes, via Jupiter, from your Phantom wallet).

Every mint below was checked against Jupiter's token API (symbol, decimals, liquidity).
"""

from dataclasses import dataclass

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6


@dataclass(frozen=True)
class Asset:
    symbol: str
    binance_pair: str
    mint: str
    decimals: int
    coingecko_id: str
    news_keywords: tuple[str, ...]


ASSETS: dict[str, Asset] = {
    a.symbol: a
    for a in [
        Asset("SOL", "SOLUSDT", "So11111111111111111111111111111111111111112", 9, "solana",
              ("solana", "sol ")),
        Asset("BTC", "BTCUSDT", "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij", 8, "bitcoin",
              ("bitcoin", "btc")),
        Asset("ETH", "ETHUSDT", "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", 8, "ethereum",
              ("ethereum", "ether ", "eth ")),
        Asset("JUP", "JUPUSDT", "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", 6, "jupiter-exchange-solana",
              ("jupiter", "jup ")),
        Asset("BONK", "BONKUSDT", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", 5, "bonk",
              ("bonk",)),
        Asset("WIF", "WIFUSDT", "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", 6, "dogwifcoin",
              ("dogwifhat", "wif ")),
        Asset("RAY", "RAYUSDT", "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R", 6, "raydium",
              ("raydium",)),
        Asset("JTO", "JTOUSDT", "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL", 9, "jito-governance-token",
              ("jito",)),
        Asset("PYTH", "PYTHUSDT", "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3", 6, "pyth-network",
              ("pyth",)),
    ]
}
