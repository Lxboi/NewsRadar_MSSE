# Table de features v1 (spécification)

Une ligne par `(actif, seconde)` : marché, flux d'ordres et actualités
sont alignés sur le même instant. Implémentation :
[`12_feature_engine.md`](12_feature_engine.md).

## Colonnes

```text
timestamp, price
returns_1s, returns_5s, returns_10s, returns_30s, returns_60s
volatility, spread
obi_1, obi_5, obi_10, obi_25, microprice_delta
trade_imbalance, buy_volume, sell_volume, trade_velocity
sweep_score, absorption_score, liquidity_change, liquidity_imbalance
news_score, news_importance, news_novelty, news_age, news_credibility
market_regime   (LOW_VOL | NORMAL_VOL | HIGH_VOL | EXTREME_VOL)
```

## Cibles

Pour chaque horizon `h` (30, 60, 180, 300 s) et chaque seuil `b` (5 à
300 pb) :

```text
target_{h}s_up_{b}bps   = 1 si le rendement sur h ≥ +b pb
target_{h}s_down_{b}bps = 1 si le rendement sur h ≤ -b pb
```

## Règles anti-fuite d'information

1. Une feature au temps `t` n'utilise que des données reçues avant `t`.
2. Les cibles regardent le futur : ce sont des étiquettes, jamais des
   features.
3. Toutes les fenêtres glissantes regardent vers le passé uniquement.
4. `news_age` = secondes depuis la **réception** de l'actualité.
