# Base SQLite

Référence : `src/storage/database.py` (modèles SQLAlchemy).

SQLite contient ce qu'on cherche par identifiant, actif ou période. Les
ticks restent en Parquet (voir [`05_parquet_schema.md`](05_parquet_schema.md)).

| Table | Contenu |
|---|---|
| `news_events` | Actualités notées et dédupliquées, avec heure de publication **et** heure de réception |
| `market_data` | Instantanés de prix (last, mid, bid, ask) |
| `orderbook_features` | Spread, OBI, microprice, liquidité |
| `trade_features` | Flux de transactions par fenêtre |
| `signals` | **Tous** les signaux, notifiés ou non, avec leur résultat après coup |
| `orders` / `fills` | Ordres et exécutions, avec slippage et latence |
| `positions` | Positions fermées, PnL brut, frais, slippage, PnL net |
| `performance` | Bilans périodiques |

## Règles

- **Pas d'information future** : toute jointure actualités × marché
  utilise l'heure de **réception**, jamais l'heure de publication.
- **Traçabilité** : chaque signal garde la ligne de features complète et
  la version du modèle qui l'a produit.
- **Fuseaux horaires** : un type `UTCDateTime` force l'UTC à l'écriture
  et à la lecture (SQLite ne le fait pas seul).
