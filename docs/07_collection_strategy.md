# Stratégie de collecte

## Ordre de mise en place

1. Hyperliquid seul (BTC, ETH, SOL), jusqu'à une collecte fiable.
2. Moteur de carnet d'ordres sur ce flux.
3. Stockage Parquet + SQLite, pour tout pouvoir rejouer.
4. Actualités, en commençant par le RSS (le plus simple et le plus sûr).
5. Vérification que toutes les horloges sont en UTC avant de construire
   les features.

## Règles fixes

- Uniquement des API officielles, en respectant leurs limites et
  `robots.txt`. Aucun contournement anti-bot.
- Chaque donnée garde son heure d'origine **et** son heure de réception.
- Aucune clé API dans le code, la configuration ou les logs (`.env`
  uniquement).

## Pourquoi seulement trois actifs

Chaque actif ajouté multiplie la charge, le stockage et surtout le
nombre d'hypothèses testées, donc le risque de faux positif. On élargit
seulement après avoir validé le pipeline.
