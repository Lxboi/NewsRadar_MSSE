# Statistical Models — Implementation Notes (Phase 9)

Section 30, continuing the order of operations from
`docs/08_backtesting_strategy.md`: `baselines -> strategy hypotheses ->
statistical models -> walk-forward -> statistical validation`. This
document is the implementation record for the third link — the decisions
`src/models/feature_matrix.py`, `logistic_model.py`, `tree_model.py`,
`calibration.py`, and `model_registry.py` had to make that the Phase 1
stub docstrings didn't already pin down, plus a real scikit-learn-version
bug this phase's smoke-testing discipline caught before delivery.

## The shared row-based contract

Every Phase 7 module already speaks in `list[dict]` feature+target rows
(`FeatureEngine.build_rows()`, `compute_targets()`), and Phase 8's
`evaluation.py` and `baseline_model.py` do too. Phase 9 keeps that
convention rather than introducing a separate `(X, y)` matrix-prep step
at the call site: `LogisticDirectionModel.fit(rows)` /
`TreeDirectionModel.fit(rows)` /
`predict_proba(rows)` all take and return the same row shape everything
else in the codebase already produces and consumes. This is a deliberate
deviation from the Phase 1 stub signatures (`calibrate(model, X_val,
y_val)`, etc.), noted per-module below.

`src/models/feature_matrix.py` is new (not a Phase 1 stub — Phase 1 only
reserved the model files themselves) and is the single shared
implementation both models depend on for two things that must never
silently diverge between them:

- **`feature_columns(rows)`** — every column present across `rows` except
  `NON_FEATURE_COLUMNS` (`asset`, `timestamp` — identifiers, not signal;
  `price` — non-stationary, a coefficient/split learned on raw price
  wouldn't generalize across price regimes) and anything starting with
  `target_` (excluded *by prefix*, not an explicit name list, since which
  horizon/threshold combinations exist depends on what `compute_targets()`
  was called with — getting this wrong would leak a label into the
  feature set, a far more dangerous bug than a merely missing feature).
- **`extract_labels(rows, horizon_s, threshold_bps)`** — thin wrapper
  around `models/evaluation.py: actual_direction()`, promoted from
  private (`_actual_direction`) to public specifically so this reuse is
  possible. Baseline evaluation (Phase 8) and model training labels
  (Phase 9) now share one function for "what does up/down/flat mean for
  this row" — they cannot quietly disagree.

`rows_to_frame(rows, columns)` turns a row list into a `pandas.DataFrame`
with exactly `columns` in that order; a row missing a key gets `NaN`
rather than raising, since how to handle a missing value is a modeling
decision each model's own pipeline makes, not this function's.

## `LogisticDirectionModel` — calibrated logistic regression

One instance per `(asset, horizon_s, threshold_bps)`, matching the rest
of the codebase's one-instance-per-question convention
(`OrderBookEngine`, `FeatureEngine`, `RegimeDetector` are all
one-per-asset; a model trained to answer "30s/10bps" isn't the same
model as one trained for "300s/50bps"). Pipeline:
`ColumnTransformer` splitting numeric columns (median `SimpleImputer` —
robust to the outliers a microstructure feature table is prone to, unlike
a mean — then `StandardScaler`, since logistic regression's coefficients
and regularization are scale-sensitive and this table mixes OBI in
`[-1,1]` with return fractions two orders of magnitude smaller) from the
one categorical column (`market_regime`, one-hot with
`handle_unknown="ignore"` so an unseen regime label at inference time
doesn't raise) into a final `LogisticRegression`.

"Calibrated" in the Phase 1 module docstring refers to the *end state*
after `calibration.py`'s `fit_calibrated()` is applied — this file's own
`predict_proba()` output is the honest, un-post-processed likelihood
output, not guaranteed well-calibrated on this data by itself.

`predict_proba()` returns one `{"probability_down", "probability_neutral",
"probability_up"}` dict per row — chosen to match
`storage/database.py: Signal`'s column names exactly, so wiring model
output into a `Signal` row in a later phase needs no translation step.
`TreeDirectionModel` and `CalibratedDirectionModel` return the identical
shape for the same reason.

## `TreeDirectionModel` — gradient boosting

Same `fit(rows)`/`predict_proba(rows)` contract, same `feature_matrix.py`
feature selection and label extraction, so it can be compared against
the logistic model on equal footing (section 30's point). Uses
`HistGradientBoostingClassifier`, not plain
`GradientBoostingClassifier`/`RandomForestClassifier`:

- It natively tolerates `NaN` in numeric columns — the `ColumnTransformer`
  passes numeric columns through unimputed (no `SimpleImputer`, unlike the
  logistic model), because a tree can split around a learned "missing"
  branch at each node and a `None` feature (e.g. no relevant news yet) can
  itself be informative. Imputing first would throw that signal away.
- It's the histogram-binned implementation scikit-learn recommends for
  anything beyond toy data sizes — worth defaulting to given section 63's
  standing "keep heavy compute light" concern, even though model
  *training* happens offline/research-side, never on the Pi's real-time
  path.

## Calibration diagnostics and recalibration (`calibration.py`)

Two separate concerns, matching the Phase 1 stub's original docstring:

- **`calibrate(model, rows, horizon_s, threshold_bps)`** — diagnostics
  only, never modifies `model`. Multiclass Brier score (scikit-learn's
  `brier_score_loss` is binary-only, so this is a manual mean-sum-of-
  squared-errors-against-one-hot implementation — 0.0 is a perfect,
  fully-confident-and-correct forecast), `log_loss` (via scikit-learn,
  explicit `labels=[-1,0,1]` so a class absent from a given batch doesn't
  silently shift what the loss means), macro-averaged one-vs-rest
  `roc_auc_macro`, and a per-class reliability (`calibration_curve()`)
  curve with a quantile-to-uniform-binning fallback when a validation
  slice is too small/degenerate for the requested quantile bins.
- **`fit_calibrated(model, rows, horizon_s, threshold_bps, method=)`** —
  actual Platt (`"sigmoid"`) or isotonic recalibration via
  `CalibratedClassifierCV`, fit on a *held-out* validation set the base
  model never trained on (the caller owns that split — this function has
  no way to enforce it, same as `docs/08_backtesting_strategy.md`'s
  train/validation/out-of-sample discipline generally). Returns a
  `CalibratedDirectionModel` exposing the identical row-based
  `predict_proba()` contract, so calibrated and uncalibrated models are
  interchangeable to any caller — including `calibrate()` itself, which
  can re-run diagnostics on a `CalibratedDirectionModel` the same way.

### The scikit-learn 1.8 API removals this phase had to work around

Two things confirmed by direct API inspection against the installed
scikit-learn (1.8.0), not assumed from memory, before any of this code
was written:

- `LogisticRegression.__init__` no longer accepts a `multi_class`
  parameter at all (fully removed, not deprecated — confirmed via
  `inspect.signature`). `logistic_model.py` simply never passes it.
- `CalibratedClassifierCV(estimator, cv="prefit")` — the old pattern for
  "calibrate an already-fitted model" — is gone; passing `cv="prefit"`
  raises `sklearn.utils._param_validation.InvalidParameterError`. The
  replacement, confirmed via a working smoke test, is
  `CalibratedClassifierCV(FrozenEstimator(already_fitted_model),
  method=...)`, using `sklearn.frozen.FrozenEstimator` (introduced in
  scikit-learn 1.6 per the official changelog). `requirements.txt` is
  bumped from `scikit-learn>=1.5` to `scikit-learn>=1.6` specifically for
  this — a version pin that would silently break `calibration.py` if it
  drifted back down.

### A second, subtler removal found via this phase's own smoke-testing

`calibrate()`'s original implementation wrapped `roc_auc_score(...,
multi_class="ovr")` in `except ValueError: auc = None`, on the documented
assumption (true in older scikit-learn) that ROC-AUC being undefined —
because a class is entirely absent from the validation labels — raises
`ValueError`. Smoke-testing against a validation slice engineered to
never produce a "down" target found this is **not what scikit-learn 1.8
actually does**: it emits an `UndefinedMetricWarning` and returns `nan`
silently, without raising. The original `except ValueError` never fired,
so `roc_auc_macro` came back as a bare `nan` float — which is worse than
an exception, because `if roc_auc_macro is None` (the documented,
correct way for a caller to check "undefined") silently lets a `nan`
through, and `nan` isn't valid strict JSON.

Fixed with `_safe_roc_auc_macro()`: checks class presence in the actual
labels *before* calling `roc_auc_score` at all (`set(np.unique(y)) !=
set(CLASS_ORDER)` -> `None` immediately), and defensively also coerces an
`nan` result to `None` if one somehow still comes back, rather than
trusting whichever behavior (exception vs. silent `nan`) the installed
scikit-learn version happens to choose. Covered by
`tests/test_calibration.py:
test_calibrate_roc_auc_macro_is_none_not_nan_when_validation_set_misses_a_class`,
which reproduces the exact scenario that surfaced the bug.

## `ModelRegistry` (section 59)

Implemented as an **append-only JSONL log**, not a database table with
in-place mutation. Section 59's real requirement — track
`model_version, training_period, features, parameters, performance` per
candidate, with promotion as a manual, logged (not automatic) decision —
is an audit-trail requirement, and in-place mutation would destroy
exactly the history that makes an audit trail useful.

Two event kinds share one file, in write order:

- **`"register"`** — one immutable record per `model_version`: kind
  (`"logistic"`/`"tree"`), the exact `training_period`, `features` list
  (should be the model's own `feature_columns_`), `parameters`
  (hyperparameters), `performance` (whatever evaluation metrics apply —
  `evaluate_direction_accuracy()`'s dict, `calibrate()`'s dict, or both;
  this module doesn't prescribe a shape), optional `calibration` and
  `notes`. `register()` raises `ValueError` on a duplicate
  `model_version` — a version string is a unique identifier, not
  something silently overwritten.
- **`"promote"`** — a human (or a documented process standing in for one)
  decided `model_version` is now the live model for a
  `(asset, horizon_s, threshold_bps)` key. Requires a non-empty `reason`
  and refuses to promote a `model_version` that was never `register()`'d
  for that *exact* key — a model registered for BTC/30s/10bps can't be
  promoted as ETH/60s/25bps's live model, since those are different
  questions per `logistic_model.py`'s own one-instance-per-question
  convention. **Nothing in this codebase calls `promote()` automatically**
  — no accuracy threshold, no calibration score, nothing triggers it on
  its own. That is section 59's "no model is auto-deployed" enforced in
  code, not just documented.

`current_promoted(asset, horizon_s, threshold_bps)` resolves "what's live
right now" to the full `register()` record (not a bare version string) by
taking the most recent matching `promote` event and looking up its
record. `list_models()`/`list_promotions()` support optional
`asset`/`horizon_s`/`threshold_bps` filtering for auditing a specific
question's full history. Every read re-parses the file from disk (no
caching), so two independent `ModelRegistry` instances pointed at the
same path always agree — verified in
`tests/test_model_registry.py:
test_two_registry_instances_pointed_at_the_same_file_see_the_same_data`.

## What this phase deliberately does not include

Matching the scope boundaries `docs/13_baseline_strategies.md` already
drew for Phase 8, extended one link further down the chain:

- **No PnL, fees, slippage, Sharpe, walk-forward, or bootstrap
  confidence intervals.** A model that's directionally accurate and
  well-calibrated says nothing yet about whether trading it would have
  been profitable after realistic costs — that's
  `backtest/metrics.py`/`event_backtester.py`/`walk_forward.py`,
  Phase 10.
- **No automatic promotion.** `ModelRegistry.promote()` is a manual,
  reasoned, logged call — see above. Nothing about crossing a metric
  threshold in this phase's own code causes a promotion.
- **No feature selection / hyperparameter search.** Both models take
  every available feature column and fixed default hyperparameters
  (`DEFAULT_C`, `DEFAULT_MAX_ITER`, `DEFAULT_MAX_DEPTH`,
  `DEFAULT_LEARNING_RATE`) as constructor arguments a later phase or
  notebook can sweep — none of that sweeping happens here.

## Smoke-testing discipline

Following the same practice that caught real bugs in Phases 7 and 8:
before writing any of this phase's final code, `LogisticRegression`'s and
`CalibratedClassifierCV`'s actual installed-version signatures were
inspected directly (not assumed from training-time knowledge), and the
`FrozenEstimator` replacement pattern was verified working end-to-end
against synthetic data. After writing `calibration.py`, both `calibrate()`
and `fit_calibrated()` were run end-to-end against ~900 rows of synthetic
feature+target data (train/validation/test split, both `sigmoid` and
`isotonic` methods, both the logistic and tree models) — this is what
surfaced the `roc_auc_macro` / `nan` bug described above, which neither
writing the code carefully nor a narrowly-scoped unit test written before
the bug was known would have caught; only running the real diagnostic
path against a validation set actually missing a class did.
