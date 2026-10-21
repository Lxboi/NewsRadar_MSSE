"""Normalizes raw news payloads (RSS, Google News, social) into a single
internal news event schema (see docs/06_features_v1.md) before scoring.

Roadmap: PHASE 5. Not implemented yet — Phase 1 only wires the
architecture so imports, tests and config loading all work end to end.
"""

def normalize_news_event(raw: dict) -> dict:
    """See module docstring."""
    raise NotImplementedError("normalize_news_event is scaffolded in Phase 1; implemented in a later phase per ROADMAP.md")
