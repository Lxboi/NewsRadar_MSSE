# Stratégies de référence

Avant tout modèle, il faut des références simples : un modèle complexe
qui ne bat pas une règle d'une ligne ne vaut rien. Code :
`src/models/baseline_model.py` et `src/models/evaluation.py`.

## Les 7 références

| Référence | Règle |
|---|---|
| Momentum | Signe du rendement sur 30 s |
| OBI | Signe du déséquilibre sur 10 niveaux |
| Déséquilibre des trades | Signe de `trade_imbalance` |
| Actualités | Signe de `news_score` |
| Régime de volatilité | Momentum en marché calme, inverse du momentum en marché agité |
| Flux + momentum | `(OBI + trade_imbalance) / 2 + momentum` |
| Actualités + flux | `(OBI + trade_imbalance) / 2 + news_score` |

Les combinaisons sont de simples additions, sans pondération : choisir
des poids serait déjà un modèle à valider.

## Le test « fait mieux qu'une pièce ? »

`evaluate_direction_accuracy()` vérifie si le signe du signal prédit le
signe du mouvement plus souvent que le hasard :

- seules comptent les lignes où le signal prend position **et** où le
  prix a franchi le seuil ;
- test binomial exact unilatéral contre 50 %, pas un simple « plus de
  50 % ».

Ce test ne mesure ni les frais ni la rentabilité : c'est le rôle du
backtest.

```python
from src.storage.parquet_reader import read_partition
from src.features.feature_engine import compute_targets
from src.models.evaluation import evaluate_all_baselines

rows = read_partition("data/features", "features_v1", "2026-08-18", asset="BTC")
rows = compute_targets(rows, horizons_s=(30,), thresholds_bps=(10,))
for r in evaluate_all_baselines(rows, horizon_s=30, threshold_bps=10):
    print(r["kind"], r["accuracy"], r["beats_coin_flip"])
```
