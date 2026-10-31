# Synchronisation actualités × marché

Avant de construire les features, il fallait vérifier que toutes les
données partagent la même horloge et qu'aucune information future ne
peut fuir.

## Les horodatages

| Champ | Origine | Fuseau |
|---|---|---|
| `ts_exchange_ms` | Heure envoyée par Hyperliquid | UTC |
| `ts_received_ns` | Heure de réception locale | UTC |
| `event_time` | Heure de publication du flux RSS | UTC |
| `ingestion_time` | Heure de réception de l'actualité | UTC |
| `timestamp` (SQLite) | Heure d'écriture | UTC |

**Résultat : tout est en UTC.** `tests/test_clock_discipline.py` échoue
si un horodatage sans fuseau apparaît un jour.

## La règle de jointure

Une feature au temps `t` ne voit que les actualités avec
`ingestion_time ≤ t`. L'heure de publication peut être fausse ou
antidatée ; l'heure de réception est ce que le système a réellement vu.
Un test l'exécute sur la vraie base, avec des insertions dans le
désordre.

## Bug corrigé

SQLite perdait le fuseau horaire à la relecture : la valeur était juste,
mais la date revenait « sans fuseau ». Corrigé par un type
`UTCDateTime` qui force l'UTC à l'écriture et à la lecture, et refuse
toute date sans fuseau.

## Limite

Si l'horloge de la machine dérive, toutes les données dérivent ensemble.
Il faut donc garder la synchronisation automatique de l'heure active
(Windows le fait par défaut ; sur le Pi, `systemd-timesyncd`).
