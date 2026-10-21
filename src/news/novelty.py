"""novelty in [0,1]: how new is this information relative to what the
system has already ingested (section 23, feeds into decay in section 25).

Reuses `title_similarity()` from `deduplication.py` — the same similarity
function powers both "is this a duplicate?" (binary, thresholded) and "how
novel is this?" (continuous): novelty is `1 - similarity to the most
similar recent item`, so a near-duplicate scores novelty close to 0 without
needing a second, differently-tuned text model (section 9: don't multiply
dependencies).
"""
from __future__ import annotations

from src.news.deduplication import title_similarity


def score_novelty(news_event: dict, history) -> float:
    """`history` is an iterable of items with a `title` (dicts with a
    "title" key, or plain strings) representing recently-ingested news —
    e.g. a `DeduplicationTracker`'s internal history, or a plain list in
    tests. Returns 1.0 (fully novel) when history is empty or nothing in it
    resembles this event; approaches 0.0 as the closest match gets more
    similar."""
    title = news_event.get("title", "")
    max_sim = 0.0
    for past in history:
        past_title = past.get("title") if isinstance(past, dict) else past
        if not past_title:
            continue
        sim = title_similarity(title, past_title)
        if sim > max_sim:
            max_sim = sim
            if max_sim >= 1.0:
                break
    return max(0.0, min(1.0, 1.0 - max_sim))
