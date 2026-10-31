# Méthode : lancer et juger un test en paper trading

Le mode PAPER fait tourner tout le pipeline sur les vraies données en
direct, avec de l'argent et des exécutions **simulés**. Aucun ordre
n'atteint une plateforme.

Ce test montre si le système tourne de façon stable et si les décisions
ont du sens. Il ne prouve **pas** la rentabilité réelle (slippage réel,
réaction des autres acteurs, impact sur le marché).

## 1. Prérequis

- Quelques jours de données collectées en mode RESEARCH
  (`python -m src.main`).
- L'environnement Python installé et un fichier `.env` (copié depuis
  `.env.example`).

## 2. Construire la table de features

```bash
python -m scripts.build_feature_table --asset BTC --start 2026-08-21 --end 2026-08-28
```

À répéter pour chaque actif.

## 3. Entraîner et évaluer un modèle (sans le promouvoir)

```bash
python -m scripts.train_model --asset BTC --start 2026-08-21 --end 2026-08-28 \
    --horizon-s 60 --threshold-bps 10 --round-trip-cost-bps 9
```

Le script :

- fait une validation croisée chronologique purgée en 5 blocs ;
- refait le test au niveau des **événements** de marché et prévient si
  le résultat ne tient qu'au niveau des lignes ;
- calcule l'espérance nette de frais si `--round-trip-cost-bps` est
  donné ;
- enregistre le modèle sans le promouvoir.

Options : `--model tree` (gradient boosting), `--model news`
(actualités seules), `--calibrate` (recalibration Platt).

**Ne pas aller plus loin si** le test n'est pas significatif au niveau
des événements, ou si l'espérance nette n'est pas positive.

## 4. Promouvoir le modèle choisi

```bash
python -m scripts.train_model --asset BTC --start 2026-08-21 --end 2026-08-28 \
    --horizon-s 60 --threshold-bps 10 \
    --promote --reason "explication du choix"
```

La raison est obligatoire et reste dans `data/models/registry.jsonl`.

## 5. Activer le mode PAPER

1. `trading_mode.default: "PAPER"` dans `config/settings.yaml` ;
2. `PAPER_TRADING=true` dans `.env` ;
3. dans le bloc `paper_trading:` de `settings.yaml`, `horizon_s` et
   `threshold_bps` doivent correspondre au modèle promu.

## 6. Lancer et suivre

```bash
python -m src.main
```

- **Console** : `PAPER TRADING ENABLED` au démarrage, puis un panneau
  toutes les ~15 s.
- **SQLite** : `data/processed/crypto_radar.db`, tables `positions` et
  `performance`.
- **Telegram** : si configuré.

## 7. Aucun trade ?

Vérifier les probabilités avec `/signals`. Si elles n'approchent jamais
le seuil d'entrée (0,62), c'est le seuil qui bloque, pas l'absence de
signal. `scripts/sweep_entry_threshold.py` simule le résultat avec
d'autres seuils, sans toucher la configuration :

```bash
python -m scripts.sweep_entry_threshold --asset BTC \
    --start 2026-08-21 --end 2026-08-24 --horizon-s 60 --threshold-bps 10
```

Vérifier aussi que le seuil de mouvement est plus grand que le coût
aller-retour, sinon l'espérance ne peut jamais être positive.

## 8. Combien de temps tester

- Quelques heures pour vérifier la stabilité.
- Au moins quelques dizaines de trades avant de regarder un taux de
  réussite.
- Plusieurs régimes de marché si possible.
- Une durée du même ordre que la période d'entraînement.

## 9. Avant d'envisager le trading réel

Il faudrait au minimum : un PnL **net** positif, des résultats proches
de ceux de la validation, aucun incident, et un échantillon suffisant.
Rien n'est automatique : la décision reste manuelle.

## 10. Repartir de zéro

Redémarrer `main.py` remet le capital et les compteurs de risque à
zéro. L'historique SQLite et le registre ne sont jamais effacés : filtrer
par date pour comparer deux tests.
