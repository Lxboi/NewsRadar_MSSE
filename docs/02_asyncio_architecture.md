# Asyncio Architecture (section 10)

## Principles

1. **One task per source.** Each collector (`src/collectors/*.py`) runs as
   its own `asyncio.Task`, owns its own connection, and never blocks on
   another source.
2. **Bounded queues as the only coupling.** Collectors write normalized-ish
   raw events onto an `asyncio.Queue(maxsize=settings.asyncio.queue_maxsize)`.
   Downstream stages (`normalization` → `features` → `signals` → `strategy`
   → `execution`/`notifications`) each own a consumer task reading from the
   upstream queue and writing to the next one. Backpressure (a full queue)
   is a monitored condition, not silently dropped — see `monitoring/health.py`
   in later phases.
3. **Fault isolation.** Every collector's `run()` loop wraps its connection
   logic in reconnect-with-exponential-backoff (`config/settings.yaml:
   asyncio.reconnect`), with jitter, and a heartbeat/timeout so a silently
   dead connection is detected instead of hanging forever.
4. **Stale data is a hard stop, not a soft warning.** If no market data has
   arrived for `stale_data_timeout_s` (default 15s), the health monitor
   must be able to flip a global `trading_allowed = False` flag consumed by
   the strategy/execution layer (section 62). This wiring is stubbed in
   Phase 1's `main.py` and made real in Phase 3/12.
5. **Structured shutdown.** `main.py` installs signal handlers (SIGINT/SIGTERM)
   so `systemd` can stop the service cleanly on the Raspberry Pi (section 63).

## Task graph (Phase 1 skeleton)

```text
collector_task(hyperliquid) ─┐
collector_task(kraken)       ─┤──► raw_queue ──► normalizer_task ──► normalized_queue ──► (future stages)
collector_task(news/rss/...) ─┘
                                                                    heartbeat_task (logs liveness, checks staleness)
```

Only the Hyperliquid collector is real in Phase 1 (Phase 2 deliverable);
Kraken/news collectors are stub tasks that log "not yet implemented" and
exit cleanly rather than crash the process, so `main.py` is runnable today
end-to-end without every source existing yet.

## Why queues and not direct calls

Queues decouple producer speed from consumer speed (a slow feature
computation must not stall the WebSocket read loop and cause the exchange
to drop the connection for falling behind), and they make it trivial to
add a new source later (section 6) — a new collector just needs a `run()`
coroutine and a reference to the shared `raw_queue`.

## Concrete skeleton

See `src/main.py` for the actual wiring (real code, not a stub) and
`src/collectors/hyperliquid_ws.py` for the first concrete collector
interface, detailed in `docs/03_hyperliquid_ws_architecture.md`.
