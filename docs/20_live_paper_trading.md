# Paper trading en direct

Cette phase branche tous les modules précédents en un seul pipeline qui
tourne réellement, en mode PAPER (argent et exécutions simulés). Mode
d'emploi : [`21_paper_trading_methodology.md`](21_paper_trading_methodology.md).

## Organisation — `src/live/trading_loop.py`

- `PaperTradingEngine` : un seul par compte. Il possède le gestionnaire
  de risque, les alertes, le bot Telegram et le suivi des performances.
- `AssetTradingUnit` : un par actif. Il a ses propres features, signal,
  stratégie et exécuteur simulé.

Un actif sans modèle promu est ignoré (avec un message au démarrage),
sans bloquer les autres.

`main.py` transmet au moteur les mêmes événements de marché et
d'actualités que ceux reçus par l'affichage et le stockage, et appelle
`tick()` à intervalle régulier.

## Deux verrous pour activer le mode PAPER

1. `trading_mode.default: "PAPER"` dans `config/settings.yaml` ;
2. `PAPER_TRADING=true` dans `.env`.

Si l'un des deux manque, rien ne change par rapport au mode recherche.

Une fois activé, trois tâches s'ajoutent : évaluation de la stratégie,
enregistrement des performances dans SQLite, et un panneau dans la
console (positions, dernier signal, performances).

## Alertes

- Position ouverte ou fermée : à chaque action.
- Signal à forte conviction : proposé à chaque tick, filtré par
  l'anti-spam.
- Alerte de risque : une seule fois, au moment où le compte passe en
  blocage.

Telegram s'active seulement si le token et le chat ID sont dans `.env` ;
sinon, les alertes vont dans les logs.

## Ajouts liés à cette phase

- `scripts/train_model.py` : entraîne, évalue et enregistre un modèle.
  La promotion exige `--promote` **et** `--reason`.
- `model_persistence.py` : sauvegarde et chargement des modèles.
- Mode incrémental du moteur de features, identique au mode batch.
- Colonne `liquidity_imbalance` ajoutée : sans elle, la sortie
  « retournement de liquidité » ne se déclenchait jamais.
- Écriture des positions et des performances dans SQLite.
