# Stockage Parquet

Les données haute fréquence (ticks, features) sont en Parquet ; les
données consultées par identifiant sont en SQLite (voir
[`05_sqlite_schema.md`](05_sqlite_schema.md)). Code :
`src/storage/parquet_store.py`.

## Arborescence

```text
data/
    raw/          données brutes des collecteurs
        hyperliquid_l2/asset=BTC/date=2026-08-18/part-*.parquet
        hyperliquid_trades/asset=BTC/date=2026-08-18/part-*.parquet
        news_raw/date=2026-08-18/part-*.parquet
    processed/    données nettoyées
    features/     table de features
        features_v1/asset=BTC/date=2026-08-18/part-*.parquet
```

Partitionnement `asset=.../date=...` : un backtest sur un actif et un
jour ne lit que les fichiers nécessaires.

## `raw/hyperliquid_l2` — une ligne par niveau de carnet

| Colonne | Type | Rôle |
|---|---|---|
| `ts_exchange_ms` | int64 | Heure de la plateforme |
| `ts_received_ns` | int64 | Heure de réception locale |
| `asset` | string | |
| `side` | string | `bid` / `ask` |
| `price`, `size` | float64 | |
| `n_orders` | int64 | Nombre d'ordres au niveau |

## `raw/hyperliquid_trades` — une ligne par transaction

Mêmes horodatages, plus `side` (`B` / `A`), `price`, `size` et `tid`
(identifiant Hyperliquid).

## Conventions

- Compression `zstd`, horodatages toujours en UTC.
- Schéma : on ajoute des colonnes, on ne modifie jamais. Un changement
  incompatible crée `features_v2/` à côté de `features_v1/`, pour que
  les anciens backtests restent reproductibles.
