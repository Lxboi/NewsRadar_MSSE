# Radar d'actualités

## Sources

24 flux RSS, configurés dans `config/sources.yaml` :

- **16 médias crypto** : CoinDesk, CoinTelegraph, The Block, Decrypt,
  CryptoSlate, The Defiant, NewsBTC, Bitcoin.com News, BeInCrypto,
  AMBCrypto, Cryptonews.com, CoinPedia, DailyCoin, 99Bitcoins,
  Crypto.News, U.Today.
- **8 sources officielles** : SEC (communiqués, litiges, procédures,
  suspensions) et Fed (communiqués, politique monétaire, régulation
  bancaire, sanctions).

Non utilisés : X/Twitter (API payante), Reddit (accès sur demande) et
Google News (son `robots.txt` interdit la collecte automatique).

## Collecte

- Une tâche par flux : un site en panne ne retarde pas les autres.
- 5 requêtes simultanées au maximum, avec un décalage aléatoire.
- `robots.txt` vérifié avant la première lecture de chaque flux.
- En cas d'échec répété, le flux attend de plus en plus longtemps.

Le tout est léger : un Raspberry Pi 5 le gère sans difficulté.

## Notation de chaque actualité

| Étape | Module | Résultat |
|---|---|---|
| Classification | `classifier.py` | Catégorie : piratage, régulation, ETF, liquidation, macro, plateforme, partenariat, autre |
| Sentiment | `sentiment.py` | Score de -1 à 1 (lexique de mots-clés) |
| Pertinence | `relevance.py` | Score de 0 à 1 et actifs concernés (BTC, ETH, SOL) |
| Crédibilité | `credibility.py` | Score de 0 à 1 selon la source |
| Nouveauté | `novelty.py` | 1 − similarité avec les titres récents |
| Déduplication | `deduplication.py` | Lien vers l'événement d'origine |
| Importance | `pipeline.py` | Poids de la catégorie × pertinence |

L'impact décroît avec le temps (demi-vie de 5 minutes), à partir de
l'heure de **réception**, jamais de l'heure de publication annoncée.

## Stockage

- **Parquet** : toutes les actualités brutes.
- **SQLite** (`news_events`) : seulement celles qui touchent au moins un
  mot-clé, pour ne pas noyer la table sous les communiqués SEC sans
  rapport avec la crypto.

## Problèmes trouvés en conditions réelles (corrigés)

1. **Redirections** : CoinDesk et U.Today échouaient car `httpx` ne
   suivait pas les redirections. Corrigé.
2. **Faux positifs** : « sec » était trouvé dans « Securities », « sol »
   dans « resolve », et le nom « Securities and Exchange Commission »
   validait le mot-clé « exchange » sur chaque communiqué SEC. Corrigé
   avec une recherche par mots entiers pour les mots courts.
3. **AMBCrypto** est ignoré car son `robots.txt` l'interdit : c'est le
   comportement voulu.
4. **CryptoSlate** bloque les robots (erreur 403). On ne contourne pas :
   le flux reste simplement inactif.
