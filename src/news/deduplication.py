"""Collapses N articles covering the same primary event into one logical
event using URL, title, timestamp and text similarity (section 22).

Two entry points, sharing the same similarity function:

- `deduplicate_events()` — pure batch function over a list of events, for
  offline/research use and tests.
- `DeduplicationTracker` — streaming counterpart the live pipeline
  (`src/news/pipeline.py`) uses: checks one event at a time against a
  bounded rolling history instead of re-scanning a whole batch, so memory
  stays flat over a long-running process (Raspberry Pi, section 63).

`novelty.py` deliberately reuses `title_similarity()` from here rather than
defining its own — "near duplicate" means the same thing in both places.
"""
from __future__ import annotations

import difflib
import re
from collections import deque

DEFAULT_SIMILARITY_THRESHOLD = 0.85
DEFAULT_TIME_WINDOW_S = 900


def normalize_title(title: str) -> str:
    text = (title or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_similarity(a: str, b: str) -> float:
    """Ratio in [0, 1], 1.0 = identical after normalization. Uses stdlib
    `difflib` (no embedding model) — deliberately lightweight, per
    `sources.yaml: deduplication.use_semantic_similarity: false` and the
    project's "don't multiply dependencies on the Pi" stance (section 9)."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def deduplicate_events(
    events: "list[dict]",
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    time_window_s: float = DEFAULT_TIME_WINDOW_S,
) -> "list[dict]":
    """Pure batch function (section 22). Each event needs `event_uid`,
    `title`, `event_time` (datetime). Returns new dicts (input is not
    mutated) with a `duplicate_of` key: `None` for the canonical event in
    each cluster, else the canonical event's `event_uid`. The earliest
    event (by `event_time`) in a cluster is always canonical."""
    ordered = sorted(events, key=lambda e: e["event_time"])
    result = []
    canonical: "list[dict]" = []
    for event in ordered:
        out = dict(event)
        out["duplicate_of"] = None
        for c in canonical:
            age = abs((event["event_time"] - c["event_time"]).total_seconds())
            if age > time_window_s:
                continue
            if title_similarity(event["title"], c["title"]) >= similarity_threshold:
                out["duplicate_of"] = c["event_uid"]
                break
        if out["duplicate_of"] is None:
            canonical.append(event)
        result.append(out)
    return result


class DeduplicationTracker:
    """Streaming counterpart used by the live pipeline: checks one event at
    a time against a bounded rolling history (`max_history` most recent
    events) instead of a full batch."""

    def __init__(
        self,
        *,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        time_window_s: float = DEFAULT_TIME_WINDOW_S,
        max_history: int = 500,
    ) -> None:
        self.similarity_threshold = similarity_threshold
        self.time_window_s = time_window_s
        self._history: "deque[dict]" = deque(maxlen=max_history)

    def check_and_add(self, event: dict) -> "str | None":
        """Returns the canonical `event_uid` if `event` is a near-duplicate
        of something already seen within the time window, else `None` (and
        records `event` itself as a new canonical candidate)."""
        for c in reversed(self._history):
            age = abs((event["event_time"] - c["event_time"]).total_seconds())
            if age > self.time_window_s:
                continue
            if title_similarity(event["title"], c["title"]) >= self.similarity_threshold:
                return c["event_uid"]
        self._history.append(
            {"event_uid": event["event_uid"], "title": event["title"], "event_time": event["event_time"]}
        )
        return None
