"""market_relevance in [0,1] and affected_assets extraction (section 23).

Keyword-driven, using `config/sources.yaml: keywords` (never hardcoded
here, section 21) plus a small per-asset keyword map for `affected_assets`
extraction, kept in sync with `config/assets.yaml`'s BTC/ETH/SOL universe
(section 6) but extendable without a code change if a new asset is added.

Matching bugs found and fixed during Phase 5 live validation (2026-08-18,
real user run against the live feeds — see docs/10_news_radar.md):

1. Naive substring matching (`kw in text`) let short tickers/abbreviations
   match *inside* unrelated words — "eth" inside "together", "sol" inside
   "resolve"/"console", and worst, "sec" inside "Securities" (every single
   SEC press release's own institutional boilerplate). An SEC litigation
   release against an unrelated REIT was getting tagged `market_relevance
   > 0` and even an `affected_assets: ["SOL"]` this way — exactly the
   false-positive flooding the relevance filter exists to prevent.
2. The SEC's own name, "U.S. Securities and Exchange Commission", also
   contains the *legitimate whole word* "Exchange" — not a substring bug,
   but boilerplate contamination: every SEC feed item satisfies the
   `exchange` keyword this way regardless of actual content.

Fixed via `keyword_matches()` (word-boundary aware, with an explicit
exact-match list for ticker-style keywords) and boilerplate stripping
before matching.
"""
from __future__ import annotations

import re

DEFAULT_ASSET_KEYWORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth", "ether"],
    "SOL": ["solana", "sol"],
}

# Abbreviations/tickers that are prone to matching *inside* unrelated
# English words if only a left-word-boundary is required ("sec" prefixes
# "Securities"/"Second"/"Section"; "eth" occurs inside "together"/"method";
# "sol" inside "resolve"/"console"; "fed" prefixes "Federal"). These must
# match as a whole word (optionally with a trailing "s" for plurals like
# "ETFs"), never as a prefix. Every other keyword keeps prefix matching so
# inflected forms still count: "hack" -> "hacked"/"hacking", "regulat" ->
# "regulation"/"regulated", "sued" would otherwise also need this list, but
# it's already short+exact by nature (see EXACT_MATCH_KEYWORDS).
EXACT_MATCH_KEYWORDS = {
    "btc", "eth", "sol", "sec", "cpi", "fomc", "etf", "fed", "cftc", "gdp", "sued",
}

# Official-source boilerplate that trivially satisfies keywords ("sec",
# "exchange") on every single item from that source regardless of actual
# content — stripped before matching so relevance reflects the article's
# substance, not the publisher's own institutional name.
_BOILERPLATE_PATTERNS = [
    re.compile(r"u\.?s\.?\s+securities and exchange commission", re.IGNORECASE),
    re.compile(r"securities and exchange commission", re.IGNORECASE),
]


def strip_boilerplate(text: str) -> str:
    """Public so `classifier.py` can apply the same stripping before
    keyword-matching for categorization — an SEC release's own
    institutional name shouldn't make it classify as EXCHANGE any more
    than it should make it score as relevant."""
    for pattern in _BOILERPLATE_PATTERNS:
        text = pattern.sub(" ", text)
    return text


def keyword_matches(text: str, term: str) -> bool:
    """Case-insensitive match of `term` in `text`, word-boundary aware.
    Shared by relevance and classification (`classifier.py` imports this)
    so the same keyword means the same thing in both places."""
    escaped = re.escape(term.strip())
    if term.strip().lower() in EXACT_MATCH_KEYWORDS:
        pattern = r"\b" + escaped + r"s?\b"
    else:
        pattern = r"\b" + escaped  # left-boundary only: allows suffixes (hacked, liquidations, ...)
    return re.search(pattern, text, re.IGNORECASE) is not None


def score_relevance(
    news_event: dict,
    keywords: "list[str] | None" = None,
    asset_keywords: "dict[str, list[str]] | None" = None,
) -> dict:
    """Returns `{"market_relevance": float, "affected_assets": list[str],
    "matched_keywords": list[str]}`. `market_relevance` is 0.0 with no
    keyword hits at all (this is what keeps official SEC/Fed press releases
    about unrelated topics from polluting `news_events` — most of what
    those feeds publish has nothing to do with crypto), then rises with
    more distinct keyword hits, capped at 1.0 past 3 — one hit already
    means "relevant", more hits raise confidence rather than scaling
    linearly forever."""
    raw_text = f"{news_event.get('title', '')} {news_event.get('summary', '')}"
    text = strip_boilerplate(raw_text)
    keywords = keywords or []
    matched = [kw for kw in keywords if keyword_matches(text, kw)]
    market_relevance = min(1.0, len(matched) / 3) if matched else 0.0

    asset_kw = asset_keywords or DEFAULT_ASSET_KEYWORDS
    affected_assets = [
        asset for asset, terms in asset_kw.items() if any(keyword_matches(text, term) for term in terms)
    ]

    return {
        "market_relevance": market_relevance,
        "affected_assets": affected_assets,
        "matched_keywords": matched,
    }
