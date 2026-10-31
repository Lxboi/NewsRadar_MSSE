# Backtest

Code : `src/backtest/` (`metrics.py`, `event_backtester.py`,
`orderbook_backtester.py`, `walk_forward.py`).

## Simulation — `event_backtester.py`

- **Entrée** : une décision par ligne (direction, probabilité,
  espérance), venant d'un modèle ou d'une stratégie de référence.
- **Filtre d'entrée** : probabilité minimale, espérance **nette de
  frais** positive, spread maximal, régime autorisé.
- **Coût aller-retour** = 2 × (frais taker + slippage supposé).
- **Sortie** : à horizon fixe, sur la même ligne future que celle
  utilisée pour les cibles. PnL et étiquettes sont donc toujours
  cohérents.
- **Taille** : 1 000 $ par trade, une position ouverte à la fois.

Les stratégies de référence passent sans filtre de probabilité (elles
n'en produisent pas), mais avec les mêmes coûts.

## Métriques — `metrics.py`

| Fonction | Question posée |
|---|---|
| `compute_metrics()` | PnL brut et net, frais, taux de réussite, espérance, profit factor, Sharpe, Sortino, drawdown |
| `bootstrap_confidence_intervals()` | Le résultat est-il stable ou dû à un petit échantillon ? |
| `monte_carlo_reshuffle()` | Le drawdown dépend-il de l'ordre des trades ? |
| `probability_of_ruin()` | À quelle fréquence le capital passe-t-il sous un seuil de ruine ? |

Sharpe et Sortino sont calculés par trade, puis annualisés selon le
nombre réel de trades par an (la crypto tourne 24 h/24).

## Test sans actualités — `orderbook_backtester.py`

Supprime toutes les colonnes `news_*` pour vérifier si l'edge vient du
carnet seul. Attention : il faut les supprimer **avant** de calculer
les décisions, sinon le modèle a déjà vu les actualités.

## Walk-forward — `walk_forward.py`

- Fenêtres entraînement → validation → test, strictement dans l'ordre
  du temps, jamais aléatoires.
- Une zone tampon de la durée de l'horizon sépare chaque partie.
- Un seul entraînement par fenêtre.
- Chaque fenêtre est classée (haussière, baissière, neutre ; calme ou
  agitée ; choc d'actualité ou non) pour voir si une stratégie ne
  marche que dans un seul régime.
