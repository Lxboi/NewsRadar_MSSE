# Architecture générale

> Document de départ (phase 1). L'état final du projet est décrit dans le
> [README](../README.md) et la [feuille de route](../ROADMAP.md).

## Principe

Le système estime des **probabilités** de mouvement de prix à court
terme, par exemple `P(rendement BTC > +15 pb | données actuelles)`. Il
ne prétend jamais à la certitude et démarre toujours en mode `RESEARCH`
(aucun ordre). Un bon backtest ne suffit jamais à prouver un edge : voir
[`08_backtesting_strategy.md`](08_backtesting_strategy.md).

## Flux de données

```text
Hyperliquid WS ─┐
Flux RSS       ─┼──► files asyncio ──► normalisation ──► features
(Kraken, Google, réseaux : prévus, non utilisés)           │
                                                          signaux
                                                             │
                                                           risque
                                                             │
                                                  exécution PAPER / Kraken
                                                             │
                                                         Telegram
```

Chaque source est une tâche indépendante avec sa propre file : la panne
d'une source ne bloque jamais les autres (voir
[`02_asyncio_architecture.md`](02_asyncio_architecture.md)).

## Organisation

`config/`, `data/`, `src/` (collectors, orderbook, news, features,
models, signals, strategy, backtest, execution, notifications,
monitoring, storage, live), `tests/`, `scripts/`, `docs/`.
