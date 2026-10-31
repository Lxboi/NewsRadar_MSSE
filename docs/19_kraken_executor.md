# Client Kraken

Code : `src/execution/kraken_executor.py`. Livré **désactivé** : le code
est complet et testé, mais il ne peut pas passer d'ordre réel sans
action explicite. Aucun ordre réel n'a jamais été passé.

## Périmètre

Marché spot uniquement (`AddOrder`, `QueryOrders`, `CancelOrder`,
`Balance`, `OpenOrders`). Les contrats à terme n'ont pas été
implémentés.

## Trois verrous, tous fermés par défaut

Le client n'est actif que si **les trois** sont ouverts :

1. `dry_run=False` (par défaut `True`) ;
2. `confirm_live_trading=True` (par défaut `False`) ;
3. clé et secret API passés explicitement (le client ne lit jamais
   `.env` lui-même).

Sinon, chaque méthode renvoie une réponse valide marquée `dry_run` avec
la liste des verrous fermés. Les 6 combinaisons à deux verrous ouverts
sont testées et restent inactives.

## Détails

- Après `AddOrder`, une seule requête `QueryOrders` lit le statut. Un
  statut inconnu est traité comme « en attente », jamais comme exécuté.
- `get_position()` renvoie toujours un prix moyen vide : l'API spot ne
  fournit pas de prix de revient.
- Les codes d'actifs Kraken (ex. `XXBT` pour BTC) sont convertis, et la
  réponse brute est toujours conservée.
- Signature HMAC-SHA512 selon la documentation Kraken ; nonce
  strictement croissant.
- Les tests utilisent un faux transport : aucun test n'appelle l'API
  réelle.
