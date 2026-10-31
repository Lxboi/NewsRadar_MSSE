# Stratégie de validation

Une stratégie n'est jamais déclarée rentable sur la foi d'un seul
backtest. Elle doit passer **tous** ces contrôles :

1. **Battre les stratégies de référence** (momentum seul, OBI seul,
   actualités seules…) sur les mêmes données et avec les mêmes coûts.
2. **Résister aux coûts réels** : frais, spread, slippage, latence.
3. **Résister au walk-forward**, jamais à un découpage aléatoire, et
   par régime de marché (haussier, baissier, calme, volatil, choc
   d'actualité).
4. **Résister aux tests statistiques** : intervalles de confiance par
   bootstrap, Monte Carlo, probabilité de ruine, sensibilité aux
   paramètres.
5. **Mesurer l'avance entre plateformes** (Hyperliquid vs Kraken) au
   lieu de la supposer.

## Fonctionnement du backtest

- Rejeu chronologique des données, sans accès au futur.
- Métriques : PnL brut et net, frais, slippage, taux de réussite,
  espérance, profit factor, Sharpe, Sortino, drawdown, durée de détention.
- Chaque exécution est enregistrée avec sa configuration pour être
  reproductible.

Ordre des étapes : références → modèles → walk-forward → validation
statistique.
