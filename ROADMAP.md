# Feuille de route et journal de recherche

> **Statut final (29/08/2026) : recherche conclue, projet arrêté.**
> Un petit edge directionnel existe sur les très petits mouvements, mais
> il est 5 à 10 fois plus petit que les coûts de transaction. Aucune
> configuration testée n'a une espérance de gain positive nette de frais.

## Les phases

| # | Phase | Statut | Contenu |
|---|---|---|---|
| 1 | Architecture | ✅ | Arborescence, configuration, schémas de stockage, premiers tests |
| 2 | WebSocket Hyperliquid | ✅ validé en direct | Carnet L2, trades, bougies ; reconnexion et ping automatiques |
| 3 | Moteur de carnet d'ordres | ✅ | Spread, OBI, microprice, flux d'ordres, murs, sweeps, absorption |
| 4 | Stockage | ✅ validé en direct | Ticks bruts en Parquet, instantanés en SQLite |
| 5 | Radar d'actualités | ✅ validé en direct | 24 flux RSS, notation (catégorie, sentiment, pertinence, nouveauté, crédibilité) |
| 6 | Synchronisation | ✅ | Toutes les horloges en UTC, aucune fuite d'information future |
| 7 | Features | ✅ | Table de features rejouée tick par tick, 1 ligne par seconde |
| 8 | Stratégies de référence | ✅ | 7 stratégies simples + test « fait mieux qu'une pièce ? » |
| 9 | Modèles statistiques | ✅ | Logistique, gradient boosting, calibration, registre de modèles |
| 10 | Backtest | ✅ | Backtest chronologique, métriques, bootstrap, Monte Carlo, walk-forward |
| 11 | Signaux et stratégie | ✅ | Probabilité, espérance, confiance, règles d'entrée et de sortie |
| 12 | Risque et Telegram | ✅ | Limites de perte, kill switch, taille de position, alertes anti-spam |
| 13 | Exécution simulée | ✅ | Paper trading avec latence, slippage et exécutions partielles |
| 14 | Client Kraken | ✅ livré désactivé | Ordres spot derrière trois verrous, tous fermés par défaut |
| 15 | Paper trading en direct | ✅ | Tout le pipeline branché en mode PAPER, entraînement des modèles |
| 16 | Trading réel | ❌ annulé | Pas d'edge exploitable |
| 17 | Raspberry Pi 24/7 | ❌ annulé | Collecteur arrêté le 29/08/2026 |

Le détail technique de chaque phase est dans [`docs/`](docs/).

## Journal de recherche (phase 15)

Une fois le pipeline complet, la recherche a consisté à tester des
hypothèses une par une, chacune avec la même exigence : validation
croisée chronologique, contrôle au niveau des événements, puis espérance
nette de frais.

**25/08 — Premier lancement en PAPER.** Collecte sur le Raspberry Pi,
entraînement sur le PC. Un bug de chemin entre Windows et Linux
empêchait de charger les modèles : corrigé. Modèles 60 s / 10 pb promus
pour BTC, ETH et SOL (BTC : 60,5 % de précision, p = 5,6·10⁻⁹).

**25/08 — Aucun trade en 20 h.** Les seuils de probabilité, fixés avant
d'avoir un vrai modèle, n'étaient jamais atteints. La commande Telegram
`/signals` ne répondait pas non plus (message trop long) : corrigée.

**26/08 — Le mur des frais.** Les frais réels de Kraken (0,40 % maker,
0,80 % taker) donnent un coût aller-retour d'environ 166 pb. Avec un
seuil de 10 pb, l'espérance ne peut **jamais** être positive. Décision :
viser des mouvements plus grands (150 à 300 pb).

**26/08 — Validation croisée purgée.** Les grands mouvements sont rares
et concentrés sur quelques jours, donc une simple séparation
« passé / dernier bloc » laissait parfois zéro exemple à tester. Elle est
remplacée par une validation croisée chronologique purgée en 5 blocs.

**28/08 — Modèle « actualités seules » : rejeté.** Un premier test
semblait très significatif, mais en validation croisée le modèle ne
prédit rien (ROC-AUC 0,40 à 0,45). Cause : une même actualité reste
active pendant ~15 minutes, donc une seule actualité était comptée
comme des centaines d'observations.

**28/08 — Contrôle au niveau des événements.** Le même piège a été
retrouvé sur SOL à 100 pb : 120 des 146 « réussites » venaient de deux
courtes fenêtres. Un nouveau contrôle regroupe les lignes en événements
de marché et refait le test sur ces seuls événements. Il est désormais
intégré à `scripts/train_model.py`.

**28/08 — Balayage complet des seuils (10 à 300 pb).**

| Actif | 10 pb / 60 s | 15 pb / 300 s | 25 pb / 300 s | ≥ 50 pb |
|---|---|---|---|---|
| BTC | ✅ p = 0,002 | ✅ p = 0,002 | ✅ p = 0,008 | ❌ |
| ETH | ✅ p = 0,001 | ✅ p = 0,003 | ❌ | ❌ |
| SOL | ❌ p = 0,074 | ❌ | ❌ | ❌ |

Au-delà de 75 pb, il n'y a même plus assez d'exemples pour entraîner un
modèle.

**28/08 — Exécution moins chère.** Une DEX sur Solana n'est pas moins
chère au total (slippage, MEV, risque de bridge). En revanche,
Hyperliquid coûte 10 à 27 fois moins que Kraken (~9 pb aller-retour en
maker). Résultat : l'espérance nette reste **négative partout**. Le
meilleur cas (BTC 300 s / 25 pb) est à −0,17 pb, indiscernable de zéro
(p = 0,59).

**28/08 — Modèle à arbres (interactions actualités × carnet).** Meilleure
classification (ROC-AUC ~0,70), mais pas meilleure rentabilité : espérance
nette négative dans les 5 configurations. Il a aussi fait apparaître un
nouveau faux positif, bien détecté par le contrôle au niveau des
événements.

## Conclusion

- Un edge directionnel réel existe, mais seulement sur de très petits
  mouvements (10 à 25 pb) pour BTC et ETH.
- Il est 5 à 10 fois plus petit que les coûts sur Kraken, et reste
  négatif même sur Hyperliquid.
- Ni les actualités seules, ni un modèle capable de combiner actualités
  et carnet d'ordres ne changent ce résultat.

Deux pistes restaient possibles mais spéculatives et coûteuses à
valider (fourniture de liquidité sur AMM, règle d'entrée filtrée par les
actualités). Le projet a été volontairement arrêté à ce stade.

**État final** : phases 1 à 15 terminées, 753 tests qui passent,
collecteur Raspberry Pi arrêté. Points ouverts laissés en l'état : une
contrainte `NOT NULL` sur `performance.win_rate` et deux ambiguïtés de
configuration Telegram (voir [`docs/17`](docs/17_risk_and_telegram.md)).
