# SQLite Schema (Phase 4)

Source of truth: `src/storage/database.py` (SQLAlchemy 2.0 declarative models).
This document explains the *why*, not just the *what* — the models themselves
are the authoritative schema.

## Design principle

SQLite holds things you query by id, asset, or time range and that fit
comfortably in a row-oriented table: news events, per-window feature
summaries, signals (every one, not just notified ones — section 57), orders,
fills, positions, and periodic performance snapshots.

Tick-level order book snapshots and high-frequency feature vectors do **not**
belong in SQLite — they belong in Parquet (see `docs/05_parquet_schema.md`
— note the file numbering overlap is intentional: schema definition is one
concern split across two storage engines). SQLite would choke on
write-amplification at that frequency; Parquet handles columnar, append-heavy,
bulk-scan workloads far better.

## Tables

| Table | Purpose | Key section |
|---|---|---|
| `news_events` | Every normalized/scored/deduplicated news item, with `event_time` vs `ingestion_time` kept separate to prevent look-ahead bias | 22-25, 29 |
| `market_data` | Coarse price snapshots per asset/exchange (last/mid/bid/ask) | — |
| `orderbook_features` | Spread, OBI (1/5/10/25), microprice(+delta), liquidity metrics | 12-16 |
| `trade_features` | Order-flow metrics per rolling window (1s-180s) | 14, 17-18 |
| `signals` | **Every** signal produced, quality A/B/C/NO_TRADE, whether Telegram fired, and post-hoc outcomes at 30/60/180/300s — this is what lets us answer "does SNIPER filter out good trades?" (section 58) | 33, 57-58 |
| `orders` / `fills` | Execution records, paper or Kraken, with slippage and latency captured per fill | 41-44 |
| `positions` | Opened/closed positions with full PnL breakdown (gross, fees, slippage, net) | 44 |
| `performance` | Hourly/daily rollups feeding Telegram reports | 53-54 |

## Look-ahead safety

`news_events.event_time` and `news_events.ingestion_time` are both stored and
both indexed. Every join between news and market/feature data in
`features/feature_engine.py` must use `ingestion_time`, never `event_time` —
this is the mechanical enforcement of section 29's absolute rule.

## Reproducibility

`signals.features_snapshot` stores the full feature row (JSON) that produced
each signal, plus `model_version`, so any historical signal can be explained
after the fact ("why did the robot produce this signal?" — section 61)
without needing to replay the whole pipeline.

## Migration policy

Phase 1/4 uses `Base.metadata.create_all()` for simplicity (no migrations
framework yet). If the schema needs to change after real data has
accumulated, introduce Alembic at that point rather than hand-editing
production `.db` files.
