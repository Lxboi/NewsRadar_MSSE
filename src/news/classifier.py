"""Assigns event_type from a fixed category list (section 24).

Keyword-priority classifier: checked in the order below, first match wins.
More specific categories are checked before generic ones (e.g. an SEC
enforcement action against an exchange is REGULATION, not EXCHANGE).

Uses `relevance.py: keyword_matches()` for the same word-boundary-aware
matching `score_relevance()` uses (see that module's docstring for why —
Phase 5 live validation caught naive substring matches misfiring, e.g.
"sued" matching inside "issued"). Sharing the function keeps "does this
keyword match" consistent everywhere it's checked.

Note on scope: the master prompt's section 24 specifies *that* a fixed
category list exists; this file's exact keyword-to-category mapping is a
Phase 5 implementation choice (documented here, reviewable/extendable the
same way `rss_feeds` already is) rather than a verbatim transcription of
the prompt — flagged so it's easy to revise if it doesn't match real
article distributions once live.
"""
from __future__ import annotations

from src.news.relevance import keyword_matches, strip_boilerplate

CATEGORIES = [
    "HACK_SECURITY",
    "REGULATION",
    "ETF",
    "LIQUIDATION",
    "MACRO",
    "EXCHANGE",
    "PARTNERSHIP_ADOPTION",
    "OTHER",
]

# Iteration order is priority order (dicts preserve insertion order).
_CATEGORY_KEYWORDS = {
    "HACK_SECURITY": ["hack", "exploit", "breach", "vulnerability", "drain", "stolen", "rug pull"],
    "REGULATION": ["sec", "regulat", "lawsuit", "sued", "fomc", "compliance", "enforcement", "subpoena", "cftc"],
    "ETF": ["etf", "spot etf", "blackrock", "grayscale"],
    "LIQUIDATION": ["liquidation", "liquidated", "margin call", "deleverag"],
    "MACRO": ["fed", "federal reserve", "interest rate", "cpi", "inflation", "jobs report", "gdp", "treasury"],
    "EXCHANGE": ["binance", "coinbase", "kraken", "exchange", "listing", "delist"],
    "PARTNERSHIP_ADOPTION": ["partnership", "integrat", "adopt", "launch", "collaborat"],
}

# Rough relative "how much does this category matter" weight, used by
# src/news/pipeline.py to derive `importance` from `market_relevance`.
# REGULATION/HACK_SECURITY outrank routine EXCHANGE/PARTNERSHIP news, in
# line with what actually moves short-term crypto price (section 24).
CATEGORY_IMPORTANCE_WEIGHT = {
    "REGULATION": 1.0,
    "HACK_SECURITY": 1.0,
    "MACRO": 0.9,
    "ETF": 0.8,
    "LIQUIDATION": 0.7,
    "EXCHANGE": 0.6,
    "PARTNERSHIP_ADOPTION": 0.4,
    "OTHER": 0.3,
}


def classify_event(news_event: dict) -> str:
    raw_text = f"{news_event.get('title', '')} {news_event.get('summary', '')}"
    text = strip_boilerplate(raw_text)
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(keyword_matches(text, kw) for kw in keywords):
            return category
    return "OTHER"
