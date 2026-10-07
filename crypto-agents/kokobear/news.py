"""KOKO's news layer: free RSS feeds -> per-asset sentiment in [-1, 1].

Two scorers:
- keyword scorer (always available, no keys)
- Claude scorer (if ANTHROPIC_API_KEY is set and `anthropic` is installed); falls back to
  keywords on any error so a news outage can never block or force a trade.

News is used as a VETO / dampener, not a trigger: there is no free historical headline
archive to backtest a news-driven entry, so we only let news stop BEAR from buying a
dip that is happening for a bad reason (hack, exploit, delisting, lawsuit...).
"""

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from .assets import ASSETS

log = logging.getLogger(__name__)

FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
    "Bitcoin Magazine": "https://bitcoinmagazine.com/.rss/full/",
    "CryptoSlate": "https://cryptoslate.com/feed/",
}

# Headlines that historically precede "the dip keeps dipping".
SEVERE = ["hack", "hacked", "exploit", "drained", "rug", "delist", "insolvency", "bankrupt",
          "halted", "outage", "sec sues", "lawsuit", "fraud", "charged", "seized", "depeg",
          "attack", "vulnerability", "frozen", "investigation"]
NEGATIVE = ["crash", "plunge", "dump", "selloff", "sell-off", "slump", "bearish", "fear",
            "liquidat", "outflow", "ban", "warning", "decline", "tumble", "sinks", "drops"]
POSITIVE = ["surge", "rally", "soar", "bullish", "inflow", "approval", "approved", "etf",
            "partnership", "upgrade", "record high", "all-time high", "adoption", "launch",
            "integrat", "jumps", "gains", "breakout", "buyback"]


@dataclass
class Headline:
    source: str
    title: str
    summary: str
    published: datetime | None
    link: str

    @property
    def text(self) -> str:
        return f"{self.title}. {self.summary}".lower()


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "").strip()


def fetch_headlines(hours: int = 24, timeout: int = 15) -> list[Headline]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out: list[Headline] = []
    for source, url in FEEDS.items():
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 kokobear"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:  # one dead feed must not kill the briefing
            log.warning("feed %s failed: %s", source, e)
            continue
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            pub_raw = item.findtext("pubDate")
            try:
                pub = parsedate_to_datetime(pub_raw) if pub_raw else None
            except (TypeError, ValueError):
                pub = None
            if pub and pub.tzinfo and pub < cutoff:
                continue
            out.append(Headline(source, title, _strip_html(item.findtext("description") or "")[:400],
                                pub, item.findtext("link") or ""))
    return out


def _mentions(h: Headline, symbol: str) -> bool:
    t = f" {h.text} "
    return any(k in t for k in ASSETS[symbol].news_keywords) or f" {symbol.lower()} " in t


def _title_mentions(h: Headline, symbol: str) -> bool:
    t = f" {h.title.lower()} "
    return any(k in t for k in ASSETS[symbol].news_keywords) or f" {symbol.lower()} " in t


def keyword_scores(headlines: list[Headline]) -> dict[str, dict]:
    """Per asset + 'MARKET': {'score': -1..1, 'severe': bool, 'n': count, 'top': [titles]}.

    A coin is only flagged severe when the HEADLINE itself names the coin and a severe word;
    keyword matching is crude, so Claude scoring is preferred when available."""
    def score(hs: list[Headline], symbol: str | None = None) -> dict:
        if not hs:
            return {"score": 0.0, "severe": False, "n": 0, "top": []}
        pos = sum(any(w in h.text for w in POSITIVE) for h in hs)
        neg = sum(any(w in h.text for w in NEGATIVE) for h in hs)
        sev = [h.title for h in hs if any(w in h.title.lower() for w in SEVERE)
               and (symbol is None or _title_mentions(h, symbol))]
        raw = (pos - neg - 2 * len(sev)) / max(len(hs), 1)
        return {"score": max(-1.0, min(1.0, raw)), "severe": bool(sev), "n": len(hs),
                "top": (sev or [h.title for h in hs])[:3]}

    result = {sym: score([h for h in headlines if _mentions(h, sym)], sym) for sym in ASSETS}
    market = score(headlines)
    market["severe"] = False  # generic market negativity is a dampener, not a veto
    result["MARKET"] = market
    return result


SENTIMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "assets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "enum": list(ASSETS) + ["MARKET"]},
                    "score": {"type": "number"},
                    "severe": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["symbol", "score", "severe", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["assets"],
    "additionalProperties": False,
}


def claude_scores(headlines: list[Headline], model: str = "claude-opus-5-5") -> dict[str, dict] | None:
    """Ask Claude to read the headlines like a crypto desk analyst. Returns None if unavailable."""
    try:
        import anthropic
    except ImportError:
        return None
    if not headlines:
        return None
    lines = "\n".join(f"- [{h.source}] {h.title} — {h.summary[:200]}" for h in headlines[:150])
    prompt = (
        "You are KOKO, the research analyst for a long-only crypto dip-buying system on Solana. "
        f"Assets: {', '.join(ASSETS)} plus MARKET (overall crypto). For each asset that the headlines "
        "below say something about, plus MARKET, give a sentiment score from -1 (very bearish) to 1 "
        "(very bullish) for the next 1-2 days. Set severe=true only for asset-specific events that make "
        "buying a price dip dangerous: hacks, exploits, depegs, delistings, enforcement actions, "
        "insolvency, chain halts. Skip assets the headlines don't cover. Keep reasons to one sentence.\n\n"
        f"Headlines (last 24h):\n{lines}"
    )
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=4000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SENTIMENT_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason != "end_turn":
            log.warning("Claude news scoring stopped with %s; using keyword scores", resp.stop_reason)
            return None
        text = next(b.text for b in resp.content if b.type == "text")
        parsed = json.loads(text)
    except Exception as e:
        log.warning("Claude news scoring failed (%s); using keyword scores", e)
        return None
    out = {}
    for a in parsed["assets"]:
        out[a["symbol"]] = {"score": max(-1.0, min(1.0, float(a["score"]))), "severe": bool(a["severe"]),
                            "reason": a["reason"]}
    return out


def sentiment(hours: int = 24, use_claude: bool = True) -> tuple[dict[str, dict], list[Headline]]:
    headlines = fetch_headlines(hours)
    scores = keyword_scores(headlines)
    if use_claude:
        llm = claude_scores(headlines)
        if llm:
            for sym, v in llm.items():
                scores.setdefault(sym, {"n": 0, "top": []}).update(v, source="claude")
    return scores, headlines
