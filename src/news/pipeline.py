"""Ties classify/sentiment/relevance/credibility/novelty/dedup together
into one call so main.py has a single integration point per raw news item
(sections 22-26). Each stage stays independently testable in its own
module (see tests/test_news_scoring.py etc.) — this is composition, not
new scoring logic.

Not itself a Phase-1 stub: `src/news/{classifier,credibility,...}.py` were
scaffolded in Phase 1 as the individual sections-22-26 primitives; this
orchestrator is new in Phase 5 because main.py needs exactly one call per
raw item, not six.
"""
from __future__ import annotations

from collections import deque

from src.news.classifier import CATEGORY_IMPORTANCE_WEIGHT, classify_event
from src.news.credibility import score_credibility
from src.news.deduplication import (
    DEFAULT_SIMILARITY_THRESHOLD,
    DEFAULT_TIME_WINDOW_S,
    DeduplicationTracker,
)
from src.news.event_detector import DEFAULT_HALF_LIFE_S
from src.news.novelty import score_novelty
from src.news.relevance import score_relevance
from src.news.sentiment import score_sentiment


class NewsPipeline:
    """Stateful (holds rolling dedup/novelty history) — one instance per
    process, fed one raw event at a time by main.py's news_consumer task."""

    def __init__(
        self,
        *,
        keywords: "list[str]",
        rss_feeds: "list[dict]",
        asset_keywords: "dict[str, list[str]] | None" = None,
        dedup_similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        dedup_time_window_s: float = DEFAULT_TIME_WINDOW_S,
        default_half_life_s: float = DEFAULT_HALF_LIFE_S,
        novelty_history_size: int = 200,
    ) -> None:
        self.keywords = keywords
        self.rss_feeds = rss_feeds
        self.asset_keywords = asset_keywords
        self.default_half_life_s = default_half_life_s
        self._dedup = DeduplicationTracker(
            similarity_threshold=dedup_similarity_threshold,
            time_window_s=dedup_time_window_s,
            max_history=500,
        )
        self._novelty_history: "deque[dict]" = deque(maxlen=novelty_history_size)

    def process(self, raw_event: dict) -> "dict | None":
        """Returns a fully-scored event dict ready for `record_news_event`,
        or `None` if the raw item should be dropped outright (no title —
        malformed/empty feed entry)."""
        if not raw_event.get("title"):
            return None

        event = dict(raw_event)
        event["event_type"] = classify_event(event)
        event["sentiment"] = score_sentiment(event)

        relevance = score_relevance(event, keywords=self.keywords, asset_keywords=self.asset_keywords)
        event["market_relevance"] = relevance["market_relevance"]
        event["affected_assets"] = relevance["affected_assets"]
        event["matched_keywords"] = relevance["matched_keywords"]

        event["credibility"] = score_credibility(event.get("source", ""), feeds_config=self.rss_feeds)

        # Novelty must be computed against history BEFORE this event is
        # added to that same history, or every event would trivially find
        # itself as the most similar (novelty ~0).
        event["novelty"] = score_novelty(event, self._novelty_history)

        weight = CATEGORY_IMPORTANCE_WEIGHT.get(event["event_type"], 0.3)
        event["importance"] = round(min(1.0, weight * event["market_relevance"]), 4)

        event["duplicate_of"] = self._dedup.check_and_add(event)

        # Feed the (bounded) novelty history with every item seen, relevant
        # or not — "how new is this information" should account for
        # everything ingested, not just what passed the relevance filter.
        self._novelty_history.append({"title": event["title"], "event_time": event["event_time"]})

        # Prepared for Phase 6/7's decay-weighted signal use (event_detector.py);
        # not consumed by anything yet this phase.
        event["half_life_s"] = self.default_half_life_s
        event["initial_impact"] = event["importance"] * event["sentiment"]

        return event
