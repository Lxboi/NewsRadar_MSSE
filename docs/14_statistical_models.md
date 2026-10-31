# Modèles statistiques

Code : `src/models/`. Chaque modèle répond à une seule question :
`(actif, horizon, seuil)`, par exemple « BTC, 60 s, 10 pb ».

## Préparation commune — `feature_matrix.py`

- Toutes les colonnes sont des features, sauf `asset`, `timestamp`,
  `price` (non stationnaire) et tout ce qui commence par `target_`
  (pour qu'aucune cible ne fuie dans les features).
- Les étiquettes (hausse / baisse / neutre) viennent de la même
  fonction que celle des stratégies de référence.

## Les modèles

| Modèle | Fichier | Détails |
|---|---|---|
| Régression logistique | `logistic_model.py` | Valeurs manquantes remplacées par la médiane, normalisation, régime en one-hot |
| Gradient boosting | `tree_model.py` | `HistGradientBoostingClassifier`, gère seul les valeurs manquantes et les interactions |
| Actualités seules | `news_model.py` | Logistique limitée aux 5 colonnes d'actualités |

Tous renvoient `probability_up`, `probability_down` et
`probability_neutral`.

## Calibration — `calibration.py`

- `calibrate()` : diagnostic seul (score de Brier, log-loss, ROC-AUC,
  courbe de fiabilité).
- `fit_calibrated()` : recalibration Platt ou isotonique, sur des
  données que le modèle n'a pas vues.

ROC-AUC vaut `None` (et non `NaN`) quand une classe manque dans les
données de validation.

## Registre — `model_registry.py`

Un fichier JSONL en **ajout seul** : rien n'est jamais modifié ni
effacé.

- `register` : version, période d'entraînement, features, paramètres,
  performances, chemin du fichier modèle.
- `promote` : met un modèle en service pour une question donnée.
  Toujours **manuel** et avec une raison écrite. Aucun code ne promeut
  un modèle automatiquement.

Les modèles entraînés sont sauvegardés avec `joblib`
(`model_persistence.py`).
