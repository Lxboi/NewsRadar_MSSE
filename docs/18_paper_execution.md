# Exécution simulée (paper trading)

Code : `src/execution/execution_interface.py`, `paper_executor.py`,
`src/monitoring/performance.py`.

## Interface commune

`ExecutionInterface` est une classe abstraite avec 5 méthodes :
`place_order`, `cancel_order`, `get_position`, `get_balance`,
`get_open_orders`. L'exécuteur simulé et le client Kraken
l'implémentent tous les deux.

## Simulateur — `paper_executor.py`

Le backtest utilise des coûts fixes ; le simulateur ajoute de la
variabilité réaliste :

- latence tirée dans un intervalle ;
- slippage aléatoire (même moyenne que l'hypothèse fixe) ;
- probabilités de rejet et d'exécution partielle ;
- ordres limites en attente tant qu'ils ne croisent pas le marché.

Il tient son propre compte : coût moyen par actif, PnL réalisé et
latent, liquidités. Chaque décision est journalisée.

Le prix vient d'une fonction injectée (`price_lookup`), branchée sur le
carnet en direct.

## Suivi des performances — `performance.py`

Bilans par heure, par jour et depuis le début, avec les mêmes calculs
que le backtest (`compute_metrics()`). Indique aussi le meilleur actif
et le meilleur et pire type de sortie.

Bug corrigé : un trade fermé **après** l'heure du bilan pouvait y être
compté. Le filtre exige maintenant `début ≤ fermeture ≤ maintenant`.
