# Construction des features

Spécification : [`06_features_v1.md`](06_features_v1.md). Code :
`src/features/feature_engine.py`, lancé avec
`scripts/build_feature_table.py`.

## Rejeu tick par tick

La table est construite en rejouant tous les ticks bruts enregistrés
dans le même moteur de carnet d'ordres que l'affichage en direct, avec
une ligne par seconde. Un rendement sur 1 s est donc réellement observé,
pas déduit d'instantanés plus espacés.

## Aucune information future

La construction est une seule passe dans l'ordre du temps : à chaque
seconde `t`, seuls les événements reçus avant `t` ont été traités.
Les cibles sont calculées **après**, par une fonction séparée qui
n'ajoute que des colonnes `target_*` et ne touche jamais aux features.
Des tests le vérifient : ajouter ou retirer un événement futur ne
change aucune ligne passée.

## Choix par colonne

- `price` : prix médian du carnet (toujours disponible, même sans trade).
- `spread` : en points de base, pour comparer les actifs.
- `volatility` : écart-type des rendements sur les 60 dernières
  secondes.
- `trade_imbalance`, volumes, vitesse : fenêtre de 10 s.
- `sweep_score` : volume signé des sweeps des 30 dernières secondes.
- `absorption_score` : absorption acheteuse − vendeuse.
- `market_regime` : volatilité comparée à son propre historique
  (quartiles), donc sans seuil fixe par actif.
- Actualités : la dernière actualité pertinente reçue. `news_age` est
  vide tant qu'aucune actualité n'a été reçue (différent de « reçue à
  l'instant »). Une actualité macro sans actif précis s'applique à tous
  les actifs.

## Cibles

Pour chaque horizon et chaque seuil, on cherche la première ligne au
moins `h` secondes plus tard et on regarde si le prix a franchi le
seuil. En fin de données, la cible est vide plutôt qu'un faux `0`.

## Bug corrigé

Sur un marché parfaitement plat, tous les seuils de volatilité
devenaient égaux et le marché était classé « extrêmement volatil ».
Corrigé : ce cas renvoie désormais « volatilité normale ».

Le moteur a aussi un mode incrémental (`on_market_event`,
`on_news_event`, `build_row`) utilisé en direct ; un test vérifie qu'il
donne exactement les mêmes lignes que le mode batch.
