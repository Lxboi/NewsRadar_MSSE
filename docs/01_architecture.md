# Technical Architecture — Phase 1

> **Historical document.** Written at Phase 1, before any module was
> implemented. The final state of the project (Phases 1-15 built and
> tested, research concluded, project halted on 2026-08-29) is described
> in the [README](../README.md) and [ROADMAP](../ROADMAP.md).

## Purpose

This document is the Phase-1 deliverable required by section 73 of the
master prompt: technical architecture, folder layout, dependencies, install
steps, config, asyncio/WebSocket architecture, SQLite/Parquet schemas,
first feature definitions, collection/backtesting strategy, tests, and the
full roadmap. Nothing beyond scaffolding is implemented yet — see
`ROADMAP.md` for what happens in each subsequent phase, and do not start
Phase 2 without explicit validation (section 73).

## System philosophy (sections 3-5, 74)

The system estimates conditional probabilities of short-term price moves
(`P(BTC return > +15bps | data now)`), never claims certainty, and defaults
to `RESEARCH` mode. A backtest looking good is never sufficient evidence of
a real edge — see `docs/08_backtesting_strategy.md` for the validation bar.

## High-level data flow (section 10)

```text
Hyperliquid WS ─┐
Kraken WS       ─┤
News / RSS      ─┼──► ASYNC QUEUES ──► NORMALIZATION ──► FEATURE ENGINE
Google News     ─┤                                              │
Social (opt.)   ─┘                                              ▼
                                                          SIGNAL ENGINE
                                                                  │
                                                                  ▼
                                                            RISK ENGINE
                                                                  │
                                                                  ▼
                                                     PAPER / LIVE EXECUTION
                                                                  │
                                                                  ▼
                                                             TELEGRAM
```

Each collector is an independent asyncio task writing onto its own bounded
queue; one source failing (reconnect loop, backoff, stale-data timeout)
never blocks the others (section 10, detailed in
`docs/02_asyncio_architecture.md`).

## Repository layout

See the tree in section 8 of the master prompt — reproduced 1:1 under
`crypto_radar/` in this delivery: `config/`, `data/`, `src/{collectors,
normalization, orderbook, news, features, models, signals, strategy,
backtest, execution, notifications, monitoring, storage}`, `tests/`,
`notebooks/`, `scripts/`.

## What Phase 1 actually contains

- Full folder tree with `__init__.py` and stub modules (docstring +
  `NotImplementedError` placeholder) for every file named in section 8 — so
  the import graph, static analysis and test collection all work today.
- Real, non-stub content for: `config/*.yaml`, `.env.example`,
  `requirements.txt`, `src/storage/database.py` (SQLite schema), the
  asyncio queue skeleton in `src/main.py`, the Hyperliquid collector
  interface in `src/collectors/hyperliquid_ws.py` (connects, reconnects,
  heartbeats — does not yet reconstruct the order book), and the
  `tests/` skeleton.
- Documentation for every remaining Phase-1 deliverable (Parquet schema,
  feature table v1, collection strategy, backtesting strategy, roadmap).

## What Phase 1 deliberately does NOT contain

Order book reconstruction, OBI/microprice math, news scoring, models,
signal engine, strategy/risk logic, execution against Kraken, Telegram
bot — all scaffolded as interfaces only, implemented phase by phase per
`ROADMAP.md`.
