# Signaux et stratégie

Transforme les probabilités d'un modèle en signal, décide d'entrer ou
non, puis gère la position jusqu'à la sortie. Code : `src/signals/` et
`src/strategy/`.

Le backtest et le trading en direct utilisent **les mêmes fonctions**
pour la direction (`models/direction.py`) et pour les coûts
(`simulate_fill_costs()`), afin de ne jamais diverger.

## Le signal — `signal_engine.py`

Chaque signal contient : direction, probabilités, mouvement attendu,
espérance nette de frais, qualité, confiance, régime, et une copie des
features.

- **Mouvement attendu** = `p_hausse × seuil − p_baisse × seuil`
- **Espérance** = `|mouvement attendu| − coût aller-retour`

## Qualité — `scoring.py`

| Qualité | Probabilité minimale |
|---|---|
| A | 0,68 |
| B | 0,58 |
| C | 0,52 |
| NO_TRADE | en dessous, ou direction neutre |

## Confiance — `confidence.py`

Note de 0 à 1, moyenne pondérée de :

- qualité de calibration du modèle (40 %) ;
- fraîcheur des données (20 %) ;
- accord de l'OBI, du flux et des actualités avec la direction (40 %).

Une information manquante donne une valeur neutre (0,5), pas une
pénalité.

## Entrée — `entry.py`

`should_enter()` vérifie : probabilité ≥ 0,62, espérance nette > 0,
spread ≤ 5 pb, données de moins de 3 s, régime calme ou normal, et
aucun blocage par le gestionnaire de risque.

## Sorties — `exit.py`

Vérifiées dans cet ordre, la première qui se déclenche gagne :

1. objectif de gain fixe ;
2. stop fixe ;
3. stop selon la volatilité ;
4. retournement de la probabilité ;
5. retournement du flux d'ordres ;
6. retournement de la liquidité ;
7. limite de temps.

Une sortie dont les données manquent est simplement ignorée.

## Orchestration — `strategy_engine.py`

À chaque nouvelle ligne : vérifie d'abord les sorties, puis ouvre une
position si la place le permet et si le signal passe le filtre.
L'exécuteur (paper ou Kraken) est optionnel : sans lui, le moteur
renvoie seulement les actions qu'il aurait prises.
