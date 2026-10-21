# News + Market Synchronization Audit (Phase 6)

Per `docs/07_collection_strategy.md`: "confirm news `ingestion_time` and
market data timestamps are on a common clock (UTC, NTP-synced) before any
feature work begins." This is a verification/audit phase, not a new
subsystem — Phase 7's `feature_engine.py` is what actually joins news and
market data; Phase 6's job is to prove that join can be built safely on
top of what Phases 2-5 already collect, and to write the tests that keep
proving it as the codebase grows.

## 1. Every timestamp in the system, audited

| Field | Source module | Derivation | Timezone |
|---|---|---|---|
| `ts_exchange_ms` (l2Book/trades) | `hyperliquid_ws.py: parse_hyperliquid_message()` | Hyperliquid's own `data["time"]`/`t["time"]`, epoch milliseconds | UTC (Hyperliquid's API is epoch-ms, inherently timezone-free — confirmed against their docs, Phase 2) |
| `ts_received_ns` (l2Book/trades) | `hyperliquid_ws.py: _handle_message()` | `time.time_ns()` — local wall-clock at receipt, epoch nanoseconds | UTC (Unix epoch is UTC by definition; correctness depends on the host OS clock being accurate — see §3) |
| `event_time` (news) | `rss_collector.py: normalize_entry()` / `_parse_event_time()` | `feedparser`'s `published_parsed`/`updated_parsed`, normalized to UTC by feedparser itself; falls back to `ingestion_time` if unparseable | UTC (feedparser guarantee) |
| `ingestion_time` (news) | `rss_collector.py: poll_feed_once()` | `dt.datetime.now(dt.timezone.utc)` — local wall-clock at fetch time | UTC, explicitly tz-aware |
| `timestamp` (`MarketData`/`OrderbookFeatures`/`TradeFeatures`) | `main.py: sqlite_snapshot_writer()` | `dt.datetime.now(dt.timezone.utc)` | UTC, explicitly tz-aware |

**Finding: every timestamp in the system is already UTC**, either by
construction (`dt.timezone.utc` passed explicitly) or by the upstream
API's own convention (Hyperliquid epoch-ms, feedparser's UTC
normalization). There is no naive (timezone-unaware) `datetime.now()` or
local-time usage anywhere in the collection or storage path — verified by
`tests/test_clock_discipline.py`, which will fail if a future change
introduces one.

## 2. The join contract (section 29, no-look-ahead)

Restating `docs/06_features_v1.md` rule 1, now validated rather than just
asserted: **a feature computed at `timestamp = t` may only see news with
`ingestion_time <= t`, never filtered on `event_time`.** `event_time` is
self-reported by the source (a publish date in an RSS feed) and can be
backdated, stale, or simply wrong; `ingestion_time` is what this system
actually observed and is the only timestamp safe to gate on.

`tests/test_clock_discipline.py::test_asof_news_visibility_*` exercises
this directly against the real `NewsEvent` schema and `record_news_event`
insert path (not a mock) — synthetic news events are written to a
temporary SQLite DB with deliberately shuffled insertion order and
event_times that disagree with ingestion_times, and the test asserts that
"which news is visible as of time t" depends only on `ingestion_time` and
is invariant to insertion order. This is the property Phase 7's feature
engine will rely on.

## 3. What Phase 6 does *not* cover: OS clock accuracy

The audit above shows every application-level timestamp is UTC — but
`ts_received_ns` and `ingestion_time` both ultimately trust the host
machine's system clock to actually *be* correct. If the OS clock drifts
(no NTP sync, or NTP misconfigured), both market and news `ingestion_time`
drift together by the same amount — the two sources would still be
mutually consistent (still "on a common clock" relative to each other),
but that shared clock would itself be offset from true UTC, corrupting any
comparison against external ground truth (e.g., matching a news timestamp
against another data provider, or an exchange's own `ts_exchange_ms`).

This is deliberately **not** built here. `src/monitoring/system_monitor.py`
is explicitly scoped to Phase 17 (Raspberry Pi 24/7 deployment) for
CPU/RAM/disk/clock-sync monitoring — building NTP-drift detection now
would duplicate that work and give Phase 17 a component to reconcile with
rather than own outright. In the meantime:

- **Windows (current dev machine)**: Windows syncs time automatically by
  default (Settings → Time & Language → Date & Time → "Set time
  automatically"). Worth a one-time check, not a code concern.
- **Raspberry Pi (target deployment, section 63)**: `systemd-timesyncd` (or
  `chrony`) must be enabled and actually synced before relying on
  `crypto_radar` for anything time-sensitive. Phase 17's `SystemMonitor`
  is where this becomes an automated, monitored check
  (`config/settings.yaml: monitoring.watch` already lists `clock_sync` as
  a placeholder for exactly this).

## 4. Result

The audit found and fixed one real bug: SQLite has no native datetime
type, and SQLAlchemy's plain `DateTime(timezone=True)` column type does
not enforce tz-preservation on that backend the way it does on Postgres.
A verification script writing a tz-aware `NewsEvent.event_time`/
`ingestion_time` and reading it straight back showed `tzinfo=None` on
every field — the *value* (the wall-clock instant) was correct, but the
tz-aware contract this whole audit exists to guarantee was silently not
being enforced by the schema at all. Anyone who later compared a
freshly-created `datetime.now(timezone.utc)` against a value read back
from the DB would have hit `TypeError: can't compare offset-naive and
offset-aware datetimes` — or, worse, both sides could end up naive and
compare *silently* with no guarantee either was really UTC.

Fixed with a custom `UTCDateTime` `TypeDecorator` in
`src/storage/database.py`: it converts to UTC and strips tzinfo before
storing (SQLite's only option), re-attaches `tzinfo=UTC` on read, and
raises `ValueError` if a naive datetime is ever passed in — so the
tz-aware contract is enforced at the schema level instead of depending on
every caller remembering it. All 11 `DateTime(timezone=True)` columns in
the schema (`NewsEvent`, `MarketData`, `OrderbookFeatures`,
`TradeFeatures`, `Signal`, `Order`, `Fill`, `Position`,
`PerformanceSnapshot`) were switched to `UTCDateTime`. Verified via a
round-trip script through a real SQLite file (confirms `tzinfo == UTC`
after read-back) and the full test suite (172 tests, no regressions —
no existing code constructed these rows with naive datetimes, so the new
`ValueError` guard doesn't break anything that was working).

What Phase 6 adds, beyond that fix: `tests/test_clock_discipline.py`,
which turns "we believe every timestamp is UTC and the join is
look-ahead-safe" into "this is mechanically checked on every test run"
(including a regression test for the exact SQLite bug above), and this
document, which gives Phase 7 a single place to point to instead of
re-deriving the argument.
