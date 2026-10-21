# Backtesting & Validation Strategy (sections 44-46, Phase 9-10)

## What "validated" means here

A strategy is never called profitable because one backtest looked good
(section 4). It clears the bar only after all of the following:

1. **Beats its baselines** (section 31) — momentum-only, OBI-only, trade-
   imbalance-only, news-only, and simple combinations — on the same data,
   same costs, same period. A complex model that doesn't beat a one-line
   baseline is not worth deploying.
2. **Survives realistic costs** (section 37): taker/maker fees, spread,
   slippage, funding, and an explicit execution-latency penalty — not just
   gross PnL.
3. **Survives walk-forward, not a random split** (section 45): rolling
   train → validation → out-of-sample windows, evaluated separately across
   bull, bear, sideways, high-vol, low-vol, and news-shock regimes. A
   strategy that only works in one regime is a regime detector wearing a
   strategy's clothes — label it as such.
4. **Survives statistical stress-testing** (section 46): bootstrap
   confidence intervals on the PnL/Sharpe estimate, Monte Carlo trade
   reshuffling to check the result isn't order-dependent luck, a probability-
   of-ruin estimate, and a sensitivity analysis on the strategy's own
   parameters (a strategy that's only profitable at one exact threshold
   value is overfit, not robust).
5. **Cross-market lead/lag is quantified, not assumed** (section 32): if
   Hyperliquid is the signal source and Kraken the execution venue, measure
   whether HL actually leads Kraken, by how much, how stably, and whether
   that lead survives the round-trip latency to Kraken. If no advantage is
   demonstrated, use Kraken data for execution-side signals directly.

## Backtest mechanics (section 44)

- Chronological replay of `market data + order book + trades + news` from
  the `historical/` Parquet store (same schema as `processed/`, so backtest
  and live code paths share logic) — no shuffled or randomly sampled bars.
- No access to any data point after the decision timestamp — this is the
  same `event_time`/`ingestion_time` discipline as feature engineering
  (`docs/06_features_v1.md`), applied to news specifically since that's
  where look-ahead bias is easiest to accidentally introduce.
- Metrics captured per run: gross PnL, net PnL, fees, slippage, win rate,
  average win/loss, expectancy, profit factor, Sharpe, Sortino, max
  drawdown, number of trades, holding time (`src/backtest/metrics.py`).

## Order of operations (section 31 restated)

`baselines → strategy hypotheses (sections 34-36, 67) → statistical models
(section 30) → walk-forward (section 45) → statistical validation (section
46)`. A model is never promoted past this chain on the strength of a single
favorable backtest, per section 4's absolute rule.

## Reporting

Every backtest run is logged with its config (horizon, threshold, feature
version, model version, cost assumptions) so results are reproducible and
comparable across runs — mirrors the `model_registry` discipline in section
59.
