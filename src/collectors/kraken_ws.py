"""Kraken WebSocket collector (order book / trades) — used for
cross-market lead/lag analysis (section 32) and, later, execution-side
market data. Same reconnect/backoff contract as hyperliquid_ws.

Roadmap: PHASE 2 (research use) / PHASE 14 (execution use). Not implemented yet — Phase 1 only wires the
architecture so imports, tests and config loading all work end to end.
"""

class KrakenWebSocketCollector:
    """See module docstring."""

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError("KrakenWebSocketCollector is scaffolded in Phase 1; implemented in a later phase per ROADMAP.md")
