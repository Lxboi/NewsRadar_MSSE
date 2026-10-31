# Moteur de carnet d'ordres

`src/orderbook/orderbook_engine.py` orchestre ; chaque calcul est dans
son propre module, testé séparément. Une instance par actif.

## Spread et déséquilibre (OBI) — `imbalance.py`

```text
spread_pb = (meilleur_ask - meilleur_bid) / mid × 10 000
OBI       = (volume_bid - volume_ask) / (volume_bid + volume_ask)
```

Calculé sur les 1, 5, 10 et 25 premiers niveaux, plus une version
pondérée par la distance au mid.

## Microprice — `microprice.py`

```text
microprice = (bid × taille_ask + ask × taille_bid) / (taille_bid + taille_ask)
```

Plus il y a de volume côté ask, plus le microprice se rapproche de
l'ask. Sa variation et son accélération sont aussi calculées.

## Flux de transactions — `trade_flow.py`

Sur des fenêtres de 1 à 180 s : volumes acheteur et vendeur,
déséquilibre, volume signé, nombre de trades par seconde, taille
moyenne, fréquence des gros trades (5 fois la taille moyenne).
Sur Hyperliquid, `side = "B"` = acheteur agressif, `"A"` = vendeur
agressif.

## Liquidité — `liquidity.py`

Un « mur » est un niveau au moins 3 fois plus gros que la moyenne du
carnet. Indicateurs : durée de vie des murs, taux d'annulation,
variation de profondeur près du mid, et part des murs disparus en moins
de 2 s. Ce n'est **pas** un détecteur de spoofing, juste une mesure.

## Sweeps — `sweeps.py`

Au moins 3 trades du même côté en moins de 2 s. Pour chaque sweep :
volume, durée, déplacement du prix, niveaux traversés.

## Absorption — `absorption.py`

```text
absorption = volume agressif / (déplacement du prix en pb + ε)
```

Beaucoup de volume pour peu de mouvement = le carnet a absorbé la
pression. Le sens de ce signal (continuation ou retournement) est laissé
aux données.

Les seuils (×3, ×5, 2 s…) sont relatifs à chaque actif, pas des valeurs
fixes, pour fonctionner sur BTC, ETH et SOL sans réglage spécifique.
