"""Normalizes raw exchange payloads (Hyperliquid, Kraken) into a single
internal market event schema before they reach the order book engine.

Roadmap: PHASE 3. Not implemented yet — Phase 1 only wires the
architecture so imports, tests and config loading all work end to end.
"""

def normalize_market_event(raw: dict) -> dict:
    """See module docstring."""
    raise NotImplementedError("normalize_market_event is scaffolded in Phase 1; implemented in a later phase per ROADMAP.md")
