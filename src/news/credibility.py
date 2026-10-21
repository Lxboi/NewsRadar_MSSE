"""credibility in [0,1] per source, configurable, used to weight
news_score and to gate SNIPER-mode notifications.

Values come from `config/sources.yaml: rss_feeds[].credibility` — never
hardcoded per-outlet here, so adjusting a source's trust level is a config
change, not a code change (same principle as the keyword list, section 21).
"""
from __future__ import annotations

DEFAULT_CREDIBILITY = 0.5


def score_credibility(source: str, feeds_config: "list[dict] | None" = None) -> float:
    """Looks up `source` (case-insensitive, matched against `name`) in
    `feeds_config` (normally `sources.yaml`'s `rss_feeds` list). Falls back
    to `DEFAULT_CREDIBILITY` for an unknown/unconfigured source rather than
    raising — a new source showing up in a feed list shouldn't crash the
    pipeline, just get treated conservatively."""
    if feeds_config:
        needle = (source or "").strip().lower()
        for feed in feeds_config:
            if feed.get("name", "").strip().lower() == needle:
                cred = feed.get("credibility")
                if cred is not None:
                    return max(0.0, min(1.0, float(cred)))
    return DEFAULT_CREDIBILITY
