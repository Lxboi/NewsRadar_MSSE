"""Produces news_score in [-1, +1] for a normalized news event (section 23).

Deliberately a lightweight lexicon scan, not a transformer/embedding model:
section 63 keeps heavy ML off the Raspberry Pi's real-time path (model
*training*, if a proper sentiment classifier is ever built, happens on the
PC and ships as a batch-scored feature — that's later-phase territory, not
a dependency this collector needs today). This also keeps the scorer fully
explainable (section 3-4: estimate probabilities, don't hide behind a black
box) — every score traces back to which words matched.
"""
from __future__ import annotations

# Crypto-market-specific lexicon, not general-purpose sentiment words —
# e.g. "approval"/"adoption" and "hack"/"liquidation" carry far more signal
# here than generic positive/negative vocabularies would.
BULLISH_WORDS = {
    "surge", "surges", "rally", "rallies", "soar", "soars", "bullish",
    "approval", "approved", "approves", "adoption", "partnership",
    "upgrade", "breakout", "record high", "all-time high", "inflow",
    "inflows", "buy", "buying", "gain", "gains", "rebound", "recovery",
    "outperform", "milestone", "integration", "integrate", "listing",
    "listed", "expansion", "launch", "launches", "bull run",
}
BEARISH_WORDS = {
    "hack", "hacked", "exploit", "exploited", "crash", "crashes",
    "plunge", "plunges", "sell-off", "selloff", "bearish", "ban",
    "banned", "lawsuit", "sued", "fraud", "collapse", "collapses",
    "liquidation", "liquidated", "liquidations", "outflow", "outflows",
    "delist", "delisted", "breach", "vulnerability", "insolvent",
    "bankruptcy", "downgrade", "investigation", "subpoena", "halt",
    "halted", "suspension", "suspended", "drain", "drained", "rug pull",
}


def score_sentiment(news_event: dict) -> float:
    """Counts distinct lexicon hits in title+summary; score is the
    normalized (bullish - bearish) / (bullish + bearish) ratio, so it stays
    in [-1, 1] regardless of article length. Returns 0.0 (neutral) when no
    lexicon word matches at all — an explicit "no signal", not a guess."""
    text = f"{news_event.get('title', '')} {news_event.get('summary', '')}".lower()
    pos = sum(1 for w in BULLISH_WORDS if w in text)
    neg = sum(1 for w in BEARISH_WORDS if w in text)
    if pos == 0 and neg == 0:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / (pos + neg)))
