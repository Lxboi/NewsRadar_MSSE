# Data Collection Strategy (Phase 2-6)

## Sequencing (section 72)

1. **Phase 2 — Hyperliquid only**, BTC/ETH/SOL. Get raw L2 + trades flowing
   reliably (reconnect/heartbeat proven under real network conditions,
   including forced disconnects) before adding any other source.
2. **Phase 3 — Order book engine** consumes Phase 2's raw stream; no new
   collection, just processing.
3. **Phase 4 — Storage** comes online: raw Parquet + SQLite schema, so
   everything from here on is persisted and replayable.
4. **Phase 5 — News**, starting with RSS (simplest, most reliable, lowest
   ToS risk — section 72), then Google/search radar, social only if API +
   ToS genuinely allow it (section 20). Kraken WS is added here too if
   needed for lead/lag research (section 32), otherwise deferred to Phase 14.
5. **Phase 6 — Synchronization**: confirm news `ingestion_time` and market
   data timestamps are on a common clock (UTC, NTP-synced) before any
   feature work begins.

## Non-negotiables (sections 20, 29, 64)

- Official APIs and documented rate limits only. No anti-bot bypass, no
  robots.txt violations, ever.
- Every ingested item keeps both its origin timestamp and local ingestion
  timestamp — this is what makes Phase 7's no-look-ahead guarantee possible.
- No API key or secret ever appears in code, config, or logs (`.env` only).

## Quality bar before moving to Phase 5

Before adding news sources, Phase 2-4 should demonstrate, over a real
multi-day run:

- Reconnect behavior actually recovers from induced disconnects (kill the
  network, confirm resume) without manual intervention.
- No unexplained multi-minute gaps in the raw Parquet output.
- `ts_exchange` vs `ts_received` latency is small and stable (establishes a
  baseline before news adds a second, harder-to-control latency source).

## Why not 50 assets from day one (section 6)

Every asset multiplies collection load, storage, and — more importantly —
the number of hypotheses being tested, which inflates false-discovery risk
in the statistical validation phase (section 46). BTC/ETH/SOL first;
broaden only after the pipeline and at least one baseline are validated.
