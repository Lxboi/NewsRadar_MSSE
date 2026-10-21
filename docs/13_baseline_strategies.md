# Baseline Strategies — Implementation Notes (Phase 8)

Section 31, restated in `docs/08_backtesting_strategy.md`'s "order of
operations": `baselines -> strategy hypotheses -> statistical models ->
walk-forward -> statistical validation`. This document is the
implementation record for the first link in that chain — the decisions
`src/models/baseline_model.py` and `src/models/evaluation.py` had to make
that the Phase 1 stub docstrings didn't already pin down.

## Why baselines exist at all

Per section 4: a complex model that doesn't beat a one-line reference
isn't worth deploying. Every baseline here is deliberately one line of
arithmetic over Phase 7's feature columns — transparent enough that
nobody has to trust it, they can read it. That transparency is the point,
not a shortcut taken to save effort: a "baseline" with a tuned weighting
scheme or a hidden normalization step stops being a baseline and becomes
an unvalidated model wearing a baseline's name.

## The seven kinds

`baseline_model.py`'s own Phase-1 docstring already named all seven:
momentum, OBI, trade imbalance, news score, volatility regime, order
flow + momentum, news + order flow. Four are direct passthroughs of an
existing Phase 7 column (momentum = `returns_{h}s`, OBI = `obi_{level}`,
trade imbalance = `trade_imbalance`, news = `news_score`) — nothing to
decide there beyond which horizon/level to default to (see below). The
other three needed an actual definition:

- **`volatility_regime`**: `market_regime` alone carries no direction —
  "LOW_VOL" says nothing about up vs. down. Implemented as
  regime-conditional momentum instead: trend-following (pass momentum
  through unchanged) in LOW_VOL/NORMAL_VOL, mean-reversion (sign-flipped
  momentum) in HIGH_VOL/EXTREME_VOL. This is the standard microstructure
  heuristic that trend persistence and mean reversion swap places as
  realized volatility rises — not validated here (that's exactly what
  Phase 9's statistical comparison against this baseline is for), just
  encoded as the reference hypothesis.
- **`order_flow_momentum`** / **`news_order_flow`**: both defined as
  **plain unweighted addition** of an "order flow" building block
  (`(obi + trade_imbalance) / 2`, both already scaled to `[-1, 1]`) with
  momentum or `news_score` respectively. Deliberately not normalized,
  z-scored, or weighted — any such scheme would itself be a hyperparameter
  earning empirical justification (section 4 again), and picking one
  unvalidated would defeat the purpose of a baseline. If a weighted
  combination turns out to matter, that's a Phase 9 model's job to
  discover, not something to bake into the reference it's being measured
  against.

## Defaults: `momentum_horizon_s=30`, `obi_level=10`

`momentum_horizon_s` defaults to 30s because `config/strategy.yaml:
horizons_s` includes 30 as one of its five research horizons, and it's a
middle value Phase 7 always computes (`DEFAULT_RETURN_HORIZONS_S =
(1, 5, 10, 30, 60)`). `obi_level` defaults to 10 to match
`feature_engine.py: DEFAULT_TRADE_FLOW_WINDOW_S`'s 10s window, so in the
two combination baselines, the order-book side (OBI) and the executed-flow
side (`trade_imbalance`) are at least looking at roughly comparable
lookbacks rather than two arbitrarily different windows. Both are
constructor/call-time parameters, not hardcoded, so a later phase (or a
notebook) can sweep them.

## `config/strategy.yaml` only benchmarks 5 of the 7

`strategies_under_test`'s baseline entries
(`baseline_momentum_only`/`baseline_obi_only`/
`baseline_trade_imbalance_only`/`baseline_news_score_only`/
`baseline_news_plus_order_flow`) don't include standalone
`baseline_volatility_regime` or `baseline_order_flow_momentum` entries.
Both are still implemented in `baseline_model.py` because its own Phase-1
docstring committed to all 7 as the section-31 reference set, and
`evaluation.py: evaluate_all_baselines()` runs all 7 regardless of what
Phase 10's eventual comparison sweep reads from `strategy.yaml`. Left
as-is rather than silently resolved in either direction — flagged here so
it's a visible, deliberate note rather than a silent inconsistency; adding
the two missing entries to `strategy.yaml` (or explaining why they're
intentionally excluded from the benchmark set) is a config/scope decision
for whoever wires up Phase 10's sweep, not something this phase should
decide unilaterally.

## The "beats a flip of a coin" check — and what it deliberately isn't

`evaluation.py: evaluate_direction_accuracy()` answers exactly one
question: does a baseline's *sign* predict the sign of the forward move
significantly more often than chance, on an already-built Phase 7
feature+target table? Mechanics:

- A row counts only if the baseline made a directional call
  (`signal != 0`) **and** the target resolved to a clear up/down move
  (`compute_targets()`'s flat case — neither threshold cleared — is
  excluded, same convention any directional-accuracy study uses: there's
  no right answer to score a flat move against).
- Significance is a one-sided **exact binomial test** against `p=0.5`
  (`scipy.stats.binomtest`), not just "accuracy > 50%" — a small or
  noisy sample can clear 50% by chance, and the whole point of even a
  minimal check is not to rubber-stamp noise. `beats_coin_flip` is
  `False` (not `None`) when there's nothing to score — "no evidence
  found" is the honest read of an empty sample, not a claim of success.

What this explicitly is **not**, on purpose, matching Phase 9/10's
already-reserved scope (`docs/08_backtesting_strategy.md`):

- **No PnL, fees, slippage, Sharpe, or walk-forward.** A baseline
  beating a coin flip on direction says nothing about whether trading it
  would have been profitable after realistic costs — that's
  `backtest/metrics.py`/`event_backtester.py`/`walk_forward.py`,
  Phase 10, deliberately untouched here.
- **No probability calibration, Brier score, or ROC-AUC.** Those apply
  to a model that outputs a *probability* (`models/calibration.py`,
  Phase 9); a baseline outputs a raw signed score, not a probability, so
  there's nothing to calibrate.
- **No bootstrap confidence intervals or Monte Carlo reshuffling**
  (section 46) — reserved for Phase 10's full statistical validation
  suite, applied to whatever survives this much cheaper first cut.

## What running this actually looks like

There's no CLI script for this yet (unlike Phase 7's
`scripts/build_feature_table.py`) — running baselines against real
collected data is a two-line notebook/REPL job once a feature table
exists:

```python
from src.storage.parquet_reader import read_partition
from src.features.feature_engine import compute_targets
from src.models.evaluation import evaluate_all_baselines

rows = read_partition("data/features", "features_v1", "2026-08-18", asset="BTC")
rows = compute_targets(rows, horizons_s=(30,), thresholds_bps=(10,))
for result in evaluate_all_baselines(rows, horizon_s=30, threshold_bps=10):
    print(result["kind"], result["accuracy"], result["beats_coin_flip"])
```

A dedicated script is deliberately not built yet — worth adding once
there's enough real collected history to make running it more than a
smoke test, same reasoning `docs/12_feature_engine.md` used for not
wiring feature-building into `main.py`'s live loop.
