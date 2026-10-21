# Parquet Schema & Partitioning (Phase 4)

High-frequency, append-heavy, bulk-scan data lives in Parquet under `data/`,
written via `src/storage/parquet_store.py` — implemented and wired into
`src/main.py` as of Phase 4 (the layout below was originally written
speculatively in Phase 1; the `raw/hyperliquid_l2` and
`raw/hyperliquid_trades` column lists were corrected once Phase 2/3
confirmed what Hyperliquid actually sends — see the note on `sequence` vs.
`n_orders` below). SQLite (see `docs/05_sqlite_schema.md`) holds the
lower-frequency, query-by-id tables.

## Directory layout

```text
data/
    raw/          # unmodified collector output, one dataset per source, for replay/debugging
        hyperliquid_l2/asset=BTC/date=2026-08-18/part-*.parquet
        hyperliquid_trades/asset=BTC/date=2026-08-18/part-*.parquet
        kraken_l2/asset=BTC/date=2026-08-18/part-*.parquet
        news_raw/date=2026-08-18/part-*.parquet

    processed/     # normalized, deduplicated, schema-validated data
        market_events/asset=BTC/date=2026-08-18/part-*.parquet
        news_events/date=2026-08-18/part-*.parquet

    historical/     # backfilled / vendor historical data used for backtesting,
                     # same schema as processed/ so backtests and live share code paths

    features/       # output of feature_engine.py — the synchronized feature table
        features_v1/asset=BTC/date=2026-08-18/part-*.parquet
```

Partitioning is always `asset=<SYMBOL>/date=<YYYY-MM-DD>` (Hive-style), which
keeps single-asset, single-day backtests and walk-forward slices cheap to
read without scanning the whole dataset.

## `raw/hyperliquid_l2` — implemented, one row per book level

| column | type | notes |
|---|---|---|
| `ts_exchange_ms` | int64 (ms) | from the `l2Book` envelope's `time` field |
| `ts_received_ns` | int64 (ns) | local receipt timestamp — used for latency measurement |
| `asset` | string | |
| `side` | string | `"bid"` / `"ask"` |
| `price` | float64 | |
| `size` | float64 | |
| `n_orders` | int64 | Hyperliquid's `n` field (order count at that level) |

Note: the original Phase-1 spec for this table had a `sequence` column for
gap detection. Hyperliquid's confirmed `l2Book` messages (Phase 2) are full
snapshots with no sequence/checksum field, so that column doesn't apply —
`n_orders` replaced it since it's what Hyperliquid actually sends and is
useful on its own (see `docs/03_hyperliquid_ws_architecture.md`).

## `raw/hyperliquid_trades` — implemented, one row per trade

| column | type | notes |
|---|---|---|
| `ts_exchange_ms` | int64 (ms) | from each trade's `time` field |
| `ts_received_ns` | int64 (ns) | |
| `asset` | string | |
| `side` | string | `"B"` (aggressor bought) / `"A"` (aggressor sold) — see trade_flow.py's docstring |
| `price` | float64 | |
| `size` | float64 | |
| `tid` | int64 | Hyperliquid's trade id (50-bit hash of buyer/seller order ids — unique combined with `(block_time, coin)`, not globally unique alone) |

## `processed/news_events`

Mirrors the `news_events` SQLite table (see docs/05_sqlite_schema.md) but
partitioned by date for cheap bulk research queries ("all news in Q3") — the
SQLite copy remains the operational source of truth for dedup/lookup by id.

## `features/features_v1/` (feature table)

One row per `(asset, timestamp)`. Full column list is defined in
`docs/06_features_v1.md` (section 27 of the master prompt) — this file
defines *where it lives on disk*, that one defines *what's in it*. The
dataset directory is named `features_v1` (not bare `features/`) from the
start, per the schema-evolution rule directly below: a breaking column
change adds `features_v2/` alongside it rather than mutating `v1` in
place, so `v1` doesn't need renaming later to make room for that.
Written by `scripts/build_feature_table.py` (Phase 7) — see
`docs/12_feature_engine.md` for the implementation decisions behind what
that script actually computes.

## Format conventions

- Engine: `pyarrow`, compression `zstd`.
- All timestamps stored as UTC, `int64` nanoseconds or `timestamp[ns, tz=UTC]`
  — never naive/local timestamps on disk.
- File size target: roughly 64-256MB per part file; the collector/writer
  should roll to a new part rather than growing one file unbounded across a
  full day on the Raspberry Pi's limited I/O.
- Schema evolution: additive only (new nullable columns) until Phase 9+; a
  breaking change requires a new dataset version suffix (e.g. `features_v2/`)
  rather than mutating `features_v1/` in place, so old backtests stay
  reproducible.
