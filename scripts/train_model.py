"""Trains one direction model for `(asset, horizon_s, threshold_bps)`
from an already-built `features_v1` Parquet table (`scripts/
build_feature_table.py`'s output), evaluates it on a held-out
chronological validation slice, registers it in `ModelRegistry`, and
saves the fitted artifact to disk via `models/model_persistence.py`.

Roadmap: PHASE 15. New (not a Phase 1 stub) — the first script that
actually produces something `src/live/trading_loop.py` can load and run
against live data. Deliberately a one-off CLI (`scripts/`), not wired
into `main.py`'s live loop, same reasoning as `build_feature_table.py`:
training is a batch/research step you run when you want a fresh
candidate model, not a continuous process.

## Purged k-fold CV for evaluation, not a single last-block holdout

`docs/08_backtesting_strategy.md`'s discipline (section 45: chronological
splits only, purged by a full label horizon at each boundary — never a
random split, which would leak future information into training via
overlapping-horizon rows) is followed here, but this script does its own
lightweight purged k-fold, not `backtest/walk_forward.py`'s full
expanding-window/regime-labeled/PnL-backtested study. That's a
deliberate scope choice: this script's job is "produce one candidate
model to paper-trade with an honest accuracy estimate," not the full
walk-forward statistical validation `WalkForwardValidator` already
provides — running an actual walk-forward study over this same
`features_v1` table before trusting a model beyond a paper-trading
experiment is still the right next step, and is unchanged, already-built
Phase 10 functionality (`backtest/walk_forward.py`), not duplicated
here.

**Why k-fold, not a single last-block holdout (Phase 15 addendum 6):**
the original version of this script held out one fixed chronological
slice (the last `--validation-fraction` of the rows) as "validation".
That works fine when the target class is common, but for a rare,
calendar-clustered event (a `threshold_bps` large enough to clear real
trading costs turned out to fire on only 2-3 of 6 collected days for
SOL, and on some days not at all for BTC) a fixed last-block holdout has
a real chance of landing on an entirely quiet slice — reporting
`accuracy=None (n=0)` no matter how much *more* history is appended,
since new data is always appended chronologically *after* the holdout
window. `_purged_kfold_split()` instead splits the whole row range into
`--cv-folds` contiguous chronological folds and rotates which one is
held out, purging `horizon_s` seconds at both edges of the held-out fold
from every other fold's training rows (mirroring the purging
`WalkForwardValidator` already does at its own window boundaries,
generalized from one boundary to `--cv-folds - 1`). Every row gets
scored by exactly one fold's model — pooled across all folds via
`calibration.py: labels_and_proba()`/`diagnostics_from_arrays()` for a
single binomial test, ROC-AUC, Brier score, and calibration curve
computed once over the concatenated out-of-fold predictions, not
averaged per-fold (which would double-count folds with more resolved
rows the same as folds with fewer). The **final registered/saved model**
is then refit on effectively all available rows (everything except a
small held-out slice reserved for `--calibrate`, if that flag is
passed) — the CV folds exist purely to produce an honest evaluation
number, not to withhold data from the model that actually gets
promoted.

## No promotion by default (section 59)

`--promote` requires `--reason` — matching `ModelRegistry.promote()`'s
own requirement exactly. Omit `--promote` (the default) and the model is
trained, evaluated, saved to disk, and `register()`'d, but never becomes
`current_promoted()` — nothing this script does can put a model into
live paper trading without a human explicitly asking it to, here or via
a separate `python -m scripts.train_model ... --promote-only <version>`
follow-up call once its numbers have actually been reviewed.

Usage (from the project root, venv active), after `build_feature_table.py`
has produced a `features_v1` table covering the date range used here:

    python -m scripts.train_model --asset BTC --start 2026-08-18 --end 2026-08-19 \\
        --horizon-s 30 --threshold-bps 10

    python -m scripts.train_model --asset BTC --start 2026-08-18 --end 2026-08-25 \\
        --horizon-s 30 --threshold-bps 10 --model tree --calibrate \\
        --promote --reason "first paper-trading candidate, 7 days history"

    python -m scripts.train_model --asset BTC --start 2026-08-21 --end 2026-08-28 \\
        --horizon-s 60 --threshold-bps 10 --model news
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

import numpy as np
import yaml
from scipy.stats import binomtest, ttest_1samp

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

sys.path.insert(0, str(PROJECT_ROOT))

from src.features.feature_engine import find_forward_indices  # noqa: E402
from src.models.calibration import diagnostics_from_arrays, fit_calibrated, labels_and_proba  # noqa: E402
from src.models.direction import direction_from_probabilities  # noqa: E402
from src.models.evaluation import actual_direction  # noqa: E402
from src.models.logistic_model import LogisticDirectionModel  # noqa: E402
from src.models.model_persistence import save_model  # noqa: E402
from src.models.model_registry import ModelRegistry  # noqa: E402
from src.models.news_model import NewsDirectionModel  # noqa: E402
from src.models.tree_model import TreeDirectionModel  # noqa: E402
from src.storage.parquet_reader import date_range, read_partitions  # noqa: E402

logger = logging.getLogger("train_model")

MODEL_CLASSES = {"logistic": LogisticDirectionModel, "tree": TreeDirectionModel, "news": NewsDirectionModel}
DEFAULT_CALIBRATION_FRACTION = 0.25
DEFAULT_CV_FOLDS = 5


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--asset", required=True)
    p.add_argument("--start", required=True, help="Start date, YYYY-MM-DD (UTC, inclusive) of the features_v1 table to train on.")
    p.add_argument("--end", required=True, help="End date, YYYY-MM-DD (UTC, inclusive).")
    p.add_argument("--horizon-s", type=int, required=True, help="One of strategy.yaml: horizons_s.")
    p.add_argument("--threshold-bps", type=float, required=True, help="One of strategy.yaml: move_thresholds_bps.")
    p.add_argument("--model", choices=sorted(MODEL_CLASSES), default="logistic")
    p.add_argument(
        "--cv-folds", type=int, default=DEFAULT_CV_FOLDS,
        help="Number of purged chronological folds used to evaluate accuracy/calibration "
        f"(default {DEFAULT_CV_FOLDS}) — see the module docstring's 'Purged k-fold CV' section. "
        "This does not affect the final saved/registered model, which is refit on all rows "
        "(minus a small --calibrate holdout, if that flag is passed).",
    )
    p.add_argument(
        "--calibration-fraction", type=float, default=DEFAULT_CALIBRATION_FRACTION,
        help="Fraction of the (chronologically last) rows reserved as a held-out calibration set, "
        f"used only if --calibrate is passed (default {DEFAULT_CALIBRATION_FRACTION}). Ignored "
        "otherwise — without --calibrate the final model is fit on all available rows.",
    )
    p.add_argument(
        "--calibrate", action="store_true",
        help="Recalibrate with CalibratedClassifierCV (Platt scaling) on a held-out calibration "
        "split after evaluating the raw model via CV — see calibration.py: fit_calibrated(). The "
        "calibrated model, not the raw one, is what gets saved/registered when this flag is set.",
    )
    p.add_argument(
        "--round-trip-cost-bps", type=float, default=None,
        help="If set, also runs a net-of-fees expected-value check (Phase 15 addendum 10) -- both "
        "legs of a round trip's fees + slippage, in bps (e.g. Kraken taker: 166, Hyperliquid maker: "
        "~9 -- see config/strategy.yaml: costs and ROADMAP.md addendum 9/10 for sourcing). Purely "
        "informational: logged alongside the accuracy/event-level results, never affects promotion.",
    )
    p.add_argument("--promote", action="store_true", help="Promote the trained model immediately after registering it.")
    p.add_argument("--reason", default=None, help="Required with --promote (ModelRegistry.promote()'s own requirement).")
    p.add_argument("--notes", default=None)
    return p.parse_args(argv)


def _load_rows(features_root: Path, asset: str, dates: "list[str]") -> "list[dict]":
    rows = read_partitions(features_root, "features_v1", dates, asset=asset, sort_key="timestamp")
    for row in rows:
        # build_feature_table.py wrote timestamp as an ISO string (Parquet
        # has no native tz-aware datetime round-trip through
        # pa.Table.from_pylist(dict) the way this codebase writes it) —
        # parsed back here since every downstream consumer (extract_labels,
        # should_enter/should_exit, direction_from_probabilities via
        # actual_direction) expects a real datetime for arithmetic.
        row["timestamp"] = dt.datetime.fromisoformat(row["timestamp"])
    return rows


def _chronological_split(rows: "list[dict]", holdout_fraction: float, horizon_s: int) -> "tuple[list[dict], list[dict]]":
    """Train on the earlier rows, hold out the later ones — never a
    random split (section 45). Purged: rows within `horizon_s` seconds of
    the split point are dropped from *both* sides, so no training row's
    forward-looking target window can overlap into the held-out period
    (and vice versa) — the same purging principle
    `backtest/walk_forward.py: WalkForwardValidator` already applies at
    each of its own window boundaries, applied here to this script's
    single split point. Used only to carve out `--calibrate`'s
    calibration holdout now (Phase 15 addendum 6) — accuracy/calibration
    *evaluation* uses `_purged_kfold_split()` instead, since a single
    fixed last-block holdout can land on a quiet slice for a rare,
    calendar-clustered target regardless of how much history is added."""
    n = len(rows)
    split_idx = int(n * (1 - holdout_fraction))
    split_ts = rows[split_idx]["timestamp"]
    purge = dt.timedelta(seconds=horizon_s)

    train_rows = [r for r in rows[:split_idx] if r["timestamp"] <= split_ts - purge]
    holdout_rows = [r for r in rows[split_idx:] if r["timestamp"] >= split_ts + purge]
    return train_rows, holdout_rows


def _purged_kfold_split(rows: "list[dict]", k: int, horizon_s: int) -> "list[dict]":
    """Splits `rows` (already sorted ascending by `timestamp`, as every
    `features_v1` table already is) into `k` contiguous chronological
    folds, each used once as the validation fold with every other row as
    training. Purged the same way `_chronological_split()`'s single
    boundary already was, generalized to both edges of each held-out
    fold: any row within `horizon_s` seconds of *either* edge of the
    fold being validated is dropped from the training side, so no
    training row's forward-looking label window can overlap into the
    fold under evaluation.

    Unlike a single last-block holdout, every row gets a turn in exactly
    one validation fold — so a rare target event's real-world date
    doesn't decide, by luck of the calendar, whether it's ever evaluated
    at all (Phase 15 addendum 6: the failure mode this replaces — see
    the module docstring).

    Returns a list of `k` `{"fold": i, "train": [...], "validation":
    [...]}` dicts, in fold order. A fold can legitimately come back with
    an empty `train` or `validation` (e.g. `k` not dividing the data
    evenly, or purging removing everything at an edge fold) — the caller
    skips fitting/scoring for that fold rather than this function
    special-casing it away; an empty fold contributes nothing to pooled
    totals either way."""
    n = len(rows)
    purge = dt.timedelta(seconds=horizon_s)
    boundaries = [round(i * n / k) for i in range(k + 1)]

    folds = []
    for i in range(k):
        lo_idx, hi_idx = boundaries[i], boundaries[i + 1]
        if lo_idx >= hi_idx:
            folds.append({"fold": i, "train": [], "validation": []})
            continue
        lo_ts = rows[lo_idx]["timestamp"]
        hi_ts = rows[hi_idx - 1]["timestamp"]
        validation_rows = [r for r in rows[lo_idx:hi_idx] if lo_ts + purge <= r["timestamp"] <= hi_ts - purge]
        train_rows = [r for r in rows if r["timestamp"] < lo_ts - purge or r["timestamp"] > hi_ts + purge]
        folds.append({"fold": i, "train": train_rows, "validation": validation_rows})
    return folds


def _direction_score_details(model, rows: "list[dict]", horizon_s: int, threshold_bps: float) -> "list[tuple]":
    """The per-row half of directional accuracy scoring — one
    `(timestamp, correct)` tuple per scored row (both the true label and
    the model's own call decisive; see `_direction_score_counts()`).
    Factored out (Phase 15 addendum 8) so callers that need the raw
    per-row detail -- currently just `_event_level_diagnostics()`, which
    needs the timestamps to find real event boundaries -- don't have to
    re-run `predict_proba()`/re-walk the rows a second time.
    `direction_from_probabilities()`/`actual_direction()` (already-
    public, already-tested Phase 9/11 primitives) reused directly, same
    as before."""
    proba_list = model.predict_proba(rows)
    details = []
    for row, proba in zip(rows, proba_list):
        actual = actual_direction(row, horizon_s, threshold_bps)
        if actual is None or actual == 0:
            continue
        predicted, _probability = direction_from_probabilities(proba)
        if predicted == 0:
            continue
        details.append((row["timestamp"], predicted == actual))
    return details


def _direction_score_counts(model, rows: "list[dict]", horizon_s: int, threshold_bps: float) -> "tuple[int, int]":
    """The counting half of directional accuracy scoring — `(n_scored,
    n_correct)` over `rows` against `model`'s own `predict_proba()`.
    Factored out of `_directional_accuracy()` (Phase 15 addendum 6) so
    purged k-fold CV can sum counts across folds and run *one* binomial
    test over the pooled total, rather than one test per fold (which
    would be its own multiple-comparisons problem, on top of the one
    already flagged for threshold/horizon selection elsewhere). Now a
    thin wrapper over `_direction_score_details()` (Phase 15 addendum
    8) — same counts as before, just derived from the shared detail
    list instead of duplicating the walk."""
    details = _direction_score_details(model, rows, horizon_s, threshold_bps)
    return len(details), sum(1 for _ts, correct in details if correct)


def _event_level_diagnostics(scored_details: "list[tuple]", horizon_s: int) -> dict:
    """Phase 15 addendum 8. `train_model.py`'s row-level accuracy/p-value
    treats every scored row as an independent Bernoulli trial, but at
    1-second sampling a single real market move commonly stays decisive
    for many consecutive seconds — so a handful of real events can
    masquerade as hundreds of "independent" rows and produce an
    arbitrarily small p-value from almost no real evidence. Confirmed
    twice this session (a `sign(news_score)` ad hoc check, and a
    SOL/300s/100bps CV result with `n=146` that turned out to be one
    ~145-second price move): both had a nominally tiny p-value and
    turned out to be 1-2 real events repeated hundreds of times.

    This groups `scored_details` (pooled across every CV fold, each
    `(timestamp, correct)`) into clusters — consecutive scored rows less
    than `horizon_s` seconds apart are treated as the same real event,
    same order of magnitude as how long a genuine move takes to resolve
    at this horizon. Each cluster then counts as exactly *one* trial
    (majority-vote correctness across its rows; an exact tie counts as
    half a win) for a second, much more conservative binomial test.
    This does not replace the row-level test above — a real edge should
    ideally clear both — it exists so a result that only clears the
    row-level test (inflated by one repeated event) doesn't get treated
    as evidence on its own. `top_cluster_share` flags concentration even
    when the event-level test happens to still pass: one cluster
    dominating a supposedly-broad sample is itself worth a second look."""
    if not scored_details:
        return {
            "n_clusters": 0, "n_majority_correct": 0.0, "accuracy": None,
            "p_value": None, "beats_coin_flip": False, "top_cluster_share": None,
        }
    ordered = sorted(scored_details, key=lambda item: item[0])
    clusters = [[ordered[0]]]
    for item in ordered[1:]:
        if (item[0] - clusters[-1][-1][0]).total_seconds() > horizon_s:
            clusters.append([item])
        else:
            clusters[-1].append(item)

    n_majority_correct = 0.0
    for cluster in clusters:
        correct_count = sum(1 for _ts, correct in cluster if correct)
        if correct_count * 2 > len(cluster):
            n_majority_correct += 1
        elif correct_count * 2 == len(cluster):
            n_majority_correct += 0.5

    n_clusters = len(clusters)
    accuracy = n_majority_correct / n_clusters
    p_value = binomtest(round(n_majority_correct), n_clusters, p=0.5, alternative="greater").pvalue
    top_cluster_share = max(len(c) for c in clusters) / len(ordered)
    return {
        "n_clusters": n_clusters, "n_majority_correct": n_majority_correct, "accuracy": accuracy,
        "p_value": p_value, "beats_coin_flip": bool(p_value < 0.05), "top_cluster_share": top_cluster_share,
    }


def _forward_return_bps(rows: "list[dict]", horizon_s: int) -> "list[float | None]":
    """`(forward_price / row_price - 1) * 10_000` for every row, via
    `feature_engine.find_forward_indices()` — the exact same no-look-
    ahead-safe forward lookup `compute_targets()` builds its threshold
    labels from (Phase 7), reused here rather than re-derived, so a net-
    expected-value figure can never silently disagree with what
    `target_*_up/down_*bps` itself means. Unlike those columns this is
    *continuous*, not thresholded — needed because a real trade's P&L is
    the actual realized move, not just whether it cleared some bps cutoff.
    `None` wherever there's no row far enough ahead yet (end of the
    loaded range) or either row's `price` is missing/zero -- `.get()`,
    not `find_forward_prices()`'s own `rows[j]["price"]` indexing,
    deliberately: some callers' rows (e.g. this test suite's minimal
    fixtures) have no `price` key at all, and that must degrade to
    `None` here rather than raise, the same "missing data -> skip"
    convention every other feature/scoring function in this codebase
    already follows."""
    indices = find_forward_indices(rows, horizon_s)
    out: "list[float | None]" = []
    for row, j in zip(rows, indices):
        price = row.get("price")
        fwd_price = rows[j].get("price") if j is not None else None
        if fwd_price is None or price is None or price == 0:
            out.append(None)
        else:
            out.append((fwd_price / price - 1) * 10_000)
    return out


def _net_expected_value(
    rows: "list[dict]", model_cls, horizon_s: int, threshold_bps: float, k: int, round_trip_cost_bps: float,
    *, log_prefix: str = "",
) -> dict:
    """Phase 15 addendum 10. Everything above (`_cross_validate()`,
    `_event_level_diagnostics()`) answers "does the model's directional
    call beat a coin flip" — a real question, but not the one that
    decides whether a validated edge is worth *trading*: that needs the
    actual realized move in bps, netted against a real round-trip cost,
    not just correct-vs-incorrect. This runs the identical purged k-fold
    CV machinery `_cross_validate()` uses (same `_purged_kfold_split()`,
    one fresh model fit per fold, purely additive — never replaces the
    accuracy/event-level checks above, which stay the bar a result must
    clear before its economics are even worth asking about) but scores
    every fold's validation rows differently: for every row where the
    model's own call is decisive (`direction_from_probabilities()` !=
    neutral) -- regardless of whether the *true* label happened to be
    decisive, since a live strategy trades on the model's call, not on
    hindsight -- records `net_pnl_bps = predicted_direction *
    realized_forward_return_bps - round_trip_cost_bps` (a short's P&L is
    the negated return, same convention `event_backtester.py:
    simulate_fill_costs()` already uses). Pooled across folds for a
    row-level one-sample t-test (`scipy.stats.ttest_1samp`, one-sided,
    against a population mean of 0) and, via the same event-clustering
    `_event_level_diagnostics()` uses (consecutive scored rows within
    `horizon_s` seconds treated as one real event), an event-level test
    over each cluster's *mean* net_pnl_bps — the same row-vs-event
    distinction addendum 8 built, now applied to a continuous P&L figure
    instead of a binary correct/incorrect one.

    Returns `{"round_trip_cost_bps", "row_level": {...}, "event_level":
    {...}}`; each of the two sub-dicts is `{"n", "mean_net_pnl_bps",
    "p_value", "beats_breakeven"}` (or `{"n": 0, ...None...}` if nothing
    was ever scored)."""
    folds = _purged_kfold_split(rows, k, horizon_s)
    all_net_pnl: "list[tuple]" = []  # (timestamp, net_pnl_bps)

    for fold in folds:
        if not fold["train"] or not fold["validation"]:
            continue
        fold_model = model_cls(horizon_s=horizon_s, threshold_bps=threshold_bps)
        try:
            fold_model.fit(fold["train"])
        except ValueError as exc:
            logger.warning(
                "%sfold %d/%d skipped (net EV pass) -- could not fit on %d training rows (%s)",
                log_prefix, fold["fold"] + 1, len(folds), len(fold["train"]), exc,
            )
            continue

        validation_rows = fold["validation"]
        proba_list = fold_model.predict_proba(validation_rows)
        forward_returns_bps = _forward_return_bps(validation_rows, horizon_s)
        for row, proba, realized_bps in zip(validation_rows, proba_list, forward_returns_bps):
            if realized_bps is None:
                continue
            predicted, _probability = direction_from_probabilities(proba)
            if predicted == 0:
                continue
            net_pnl_bps = predicted * realized_bps - round_trip_cost_bps
            all_net_pnl.append((row["timestamp"], net_pnl_bps))

    def _one_sample_result(values: "list[float]") -> dict:
        n = len(values)
        if n == 0:
            return {"n": 0, "mean_net_pnl_bps": None, "p_value": None, "beats_breakeven": False}
        mean = float(np.mean(values))
        if n < 2 or float(np.std(values, ddof=1)) == 0.0:
            # A single trial, or every value identical -- no variance to
            # test against; report the mean honestly, no p-value implying
            # a significance test that can't actually run.
            return {"n": n, "mean_net_pnl_bps": mean, "p_value": None, "beats_breakeven": False}
        p_value = ttest_1samp(values, popmean=0.0, alternative="greater").pvalue
        return {"n": n, "mean_net_pnl_bps": mean, "p_value": float(p_value), "beats_breakeven": bool(p_value < 0.05)}

    row_level = _one_sample_result([pnl for _ts, pnl in all_net_pnl])

    if not all_net_pnl:
        event_level = {"n": 0, "mean_net_pnl_bps": None, "p_value": None, "beats_breakeven": False}
    else:
        ordered = sorted(all_net_pnl, key=lambda item: item[0])
        clusters = [[ordered[0]]]
        for item in ordered[1:]:
            if (item[0] - clusters[-1][-1][0]).total_seconds() > horizon_s:
                clusters.append([item])
            else:
                clusters[-1].append(item)
        cluster_means = [float(np.mean([pnl for _ts, pnl in cluster])) for cluster in clusters]
        event_level = _one_sample_result(cluster_means)

    return {"round_trip_cost_bps": round_trip_cost_bps, "row_level": row_level, "event_level": event_level}


def _directional_accuracy(model, rows: "list[dict]", horizon_s: int, threshold_bps: float) -> dict:
    """Same statistical test `models/evaluation.py:
    evaluate_direction_accuracy()` runs for baselines (a one-sided exact
    binomial test against p=0.5), reimplemented narrowly here against a
    fitted model's own `predict_proba()` output rather than
    `baseline_signal()`'s — that function is baseline-specific by
    construction (Phase 8) and isn't the right shared primitive to bend
    into a model-scoring helper."""
    n_scored, n_correct = _direction_score_counts(model, rows, horizon_s, threshold_bps)
    return _accuracy_result(n_scored, n_correct)


def _accuracy_result(n_scored: int, n_correct: int) -> dict:
    """Turns raw `(n_scored, n_correct)` counts — whether from one
    model/row-set or pooled across several CV folds — into the same
    accuracy/p-value/beats_coin_flip shape."""
    accuracy = (n_correct / n_scored) if n_scored else None
    p_value = binomtest(n_correct, n_scored, p=0.5, alternative="greater").pvalue if n_scored else None
    return {
        "n_scored": n_scored, "n_correct": n_correct, "accuracy": accuracy,
        "p_value": p_value, "beats_coin_flip": bool(p_value is not None and p_value < 0.05),
    }


def _cross_validate(
    rows: "list[dict]", model_cls, horizon_s: int, threshold_bps: float, k: int, *, log_prefix: str = "",
) -> dict:
    """Runs `_purged_kfold_split()`, fits one model per fold on that
    fold's training rows, scores it on that fold's validation rows, and
    pools everything (score counts for one binomial test; `(y, proba)`
    arrays for one ROC-AUC/Brier/log-loss/calibration-curve computation)
    across folds. A fold with an empty `train` or `validation` is
    skipped (logged, not raised) — same resilience convention
    `backtest/walk_forward.py: WalkForwardValidator.run()` already uses
    for an empty window. A fold whose `train` rows turn out to contain
    only one class (`sklearn`'s `ValueError` for that — the exact crash
    this replaces surfaced for) is also skipped rather than propagated:
    with a rare enough target, this can still happen to an individual
    fold even when the pooled evaluation across every fold is fine.

    Returns `{"n_folds_used": int, "n_folds_skipped": int, "accuracy":
    {...}, "calibration": {...}, "event_level": {...}}` — the first two
    accuracy/calibration dicts are exactly `_accuracy_result()`'s and
    `calibration.diagnostics_from_arrays()`'s shapes, computed over the
    pooled totals; `event_level` is `_event_level_diagnostics()`'s shape
    (Phase 15 addendum 8), computed over every fold's pooled
    `(timestamp, correct)` detail -- always check this alongside
    `accuracy` before trusting a result, especially when `n_scored` is
    small: a `beats_coin_flip=True` at the row level that doesn't also
    hold at the event level is the exact false-positive shape addendum
    8 exists to catch."""
    folds = _purged_kfold_split(rows, k, horizon_s)
    total_scored = 0
    total_correct = 0
    all_y = []
    all_proba = []
    all_details = []
    n_used = 0
    n_skipped = 0

    for fold in folds:
        if not fold["train"] or not fold["validation"]:
            n_skipped += 1
            continue
        fold_model = model_cls(horizon_s=horizon_s, threshold_bps=threshold_bps)
        try:
            fold_model.fit(fold["train"])
        except ValueError as exc:
            logger.warning(
                "%sfold %d/%d skipped -- could not fit on %d training rows (%s)",
                log_prefix, fold["fold"] + 1, len(folds), len(fold["train"]), exc,
            )
            n_skipped += 1
            continue

        details = _direction_score_details(fold_model, fold["validation"], horizon_s, threshold_bps)
        all_details.extend(details)
        total_scored += len(details)
        total_correct += sum(1 for _ts, correct in details if correct)

        y, proba = labels_and_proba(fold_model, fold["validation"], horizon_s, threshold_bps)
        if len(y):
            all_y.append(y)
            all_proba.append(proba)
        n_used += 1

    accuracy = _accuracy_result(total_scored, total_correct)
    event_level = _event_level_diagnostics(all_details, horizon_s)
    if all_y:
        pooled_y = np.concatenate(all_y)
        pooled_proba = np.concatenate(all_proba)
        calibration_diagnostics = diagnostics_from_arrays(pooled_y, pooled_proba)
    else:
        calibration_diagnostics = {"n_scored": 0, "brier_score": None, "log_loss": None, "roc_auc_macro": None, "calibration_curve": {}}

    return {
        "n_folds_used": n_used, "n_folds_skipped": n_skipped, "accuracy": accuracy,
        "calibration": calibration_diagnostics, "event_level": event_level,
    }


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
    args = parse_args(argv)
    if args.promote and not args.reason:
        raise SystemExit("--promote requires --reason (ModelRegistry.promote() itself requires a non-empty reason)")
    if args.threshold_bps == int(args.threshold_bps):
        # A real fragility, found via this script's own smoke-testing, in
        # already-tested Phase 7/9 code this script does not own or
        # change: models/evaluation.py: actual_direction() looks up
        # target columns via an f-string
        # (f"target_{horizon_s}s_up_{threshold_bps}bps"), and
        # feature_engine.py: compute_targets() always writes those
        # columns using plain int threshold values (config/strategy.yaml:
        # move_thresholds_bps is `[5, 10, 15, 25, 50]`, all whole
        # numbers). A `--threshold-bps 10` CLI float (10.0) would format
        # to "...10.0bps" and silently match zero target columns —
        # returning None for every row rather than raising, per that
        # function's own "missing target -> None" convention, which
        # surfaces several calls later as a confusing "nothing to train
        # on" error instead of the real cause. Normalized here to an int
        # whenever the value is whole (every one of the five configured
        # thresholds is), so this script's threshold_bps always matches
        # the exact key format compute_targets() actually wrote.
        args.threshold_bps = int(args.threshold_bps)

    settings = load_yaml("settings.yaml")
    storage_cfg = settings["storage"]
    features_root = PROJECT_ROOT / storage_cfg["features_dir"]
    dates = date_range(
        dt.datetime.strptime(args.start, "%Y-%m-%d").date(),
        dt.datetime.strptime(args.end, "%Y-%m-%d").date(),
    )

    logger.info("[%s] loading features_v1 rows for %s..%s", args.asset, args.start, args.end)
    rows = _load_rows(features_root, args.asset, dates)
    if not rows:
        raise SystemExit(
            f"no features_v1 rows found for asset={args.asset} in {args.start}..{args.end} under {features_root} "
            "-- run scripts/build_feature_table.py for this range first"
        )
    logger.info("[%s] %d feature rows loaded", args.asset, len(rows))

    model_cls = MODEL_CLASSES[args.model]

    logger.info("[%s] running %d-fold purged CV for evaluation...", args.asset, args.cv_folds)
    cv_result = _cross_validate(rows, model_cls, args.horizon_s, args.threshold_bps, args.cv_folds, log_prefix=f"[{args.asset}] ")
    accuracy = cv_result["accuracy"]
    diagnostics = cv_result["calibration"]
    event_level = cv_result["event_level"]
    logger.info(
        "[%s] CV (%d/%d folds used): accuracy=%s (n=%d, p=%s, beats_coin_flip=%s) roc_auc_macro=%s brier=%s",
        args.asset, cv_result["n_folds_used"], args.cv_folds,
        accuracy["accuracy"], accuracy["n_scored"], accuracy["p_value"], accuracy["beats_coin_flip"],
        diagnostics["roc_auc_macro"], diagnostics["brier_score"],
    )
    logger.info(
        "[%s] CV event-level (clusters of scored rows within %ds of each other count as 1 trial): "
        "accuracy=%s (n_clusters=%d, p=%s, beats_coin_flip=%s) top_cluster_share=%s",
        args.asset, args.horizon_s, event_level["accuracy"], event_level["n_clusters"],
        event_level["p_value"], event_level["beats_coin_flip"], event_level["top_cluster_share"],
    )
    if accuracy["beats_coin_flip"] and not event_level["beats_coin_flip"]:
        logger.warning(
            "[%s] row-level accuracy beats a coin flip but event-level does not -- this is the exact "
            "false-positive shape addendum 8 exists to catch (a handful of real events, each repeated "
            "across many consecutive scored rows, inflating the row-level p-value); do not trust this "
            "result as a real edge without independent confirmation",
            args.asset,
        )
    if args.round_trip_cost_bps is not None:
        ev_result = _net_expected_value(
            rows, model_cls, args.horizon_s, args.threshold_bps, args.cv_folds,
            args.round_trip_cost_bps, log_prefix=f"[{args.asset}] ",
        )
        row_ev, event_ev = ev_result["row_level"], ev_result["event_level"]
        logger.info(
            "[%s] net EV @ round_trip_cost=%.1fbps -- row-level: n=%d mean_net_pnl=%s p=%s "
            "beats_breakeven=%s | event-level: n=%d mean_net_pnl=%s p=%s beats_breakeven=%s",
            args.asset, args.round_trip_cost_bps,
            row_ev["n"], row_ev["mean_net_pnl_bps"], row_ev["p_value"], row_ev["beats_breakeven"],
            event_ev["n"], event_ev["mean_net_pnl_bps"], event_ev["p_value"], event_ev["beats_breakeven"],
        )
    if cv_result["n_folds_used"] == 0:
        raise SystemExit(
            f"all {args.cv_folds} CV folds were skipped (empty split side or unfittable training data on "
            "every fold) -- need more history, fewer --cv-folds, or a smaller horizon_s relative to the "
            "data span; refusing to register a model with no evaluation at all"
        )
    if cv_result["n_folds_skipped"]:
        logger.warning(
            "[%s] %d/%d CV folds were skipped (empty split side or unfittable training data) -- "
            "the pooled numbers above only reflect the %d folds that actually ran",
            args.asset, cv_result["n_folds_skipped"], args.cv_folds, cv_result["n_folds_used"],
        )

    # The final saved/registered model is refit on effectively all rows --
    # the CV folds above exist purely to produce an honest evaluation
    # number, not to withhold data from the model that actually gets
    # promoted (Phase 15 addendum 6; see module docstring). Only
    # --calibrate needs a genuine held-out slice, since
    # fit_calibrated() requires rows the final model never trained on.
    if args.calibrate:
        train_rows, calibration_rows = _chronological_split(rows, args.calibration_fraction, args.horizon_s)
        if not train_rows or not calibration_rows:
            raise SystemExit(
                "calibration split left an empty side -- need more history or a smaller --calibration-fraction"
            )
    else:
        train_rows, calibration_rows = rows, []

    model = model_cls(horizon_s=args.horizon_s, threshold_bps=args.threshold_bps)
    logger.info("[%s] fitting final %s on %d rows...", args.asset, model_cls.__name__, len(train_rows))
    model.fit(train_rows)

    final_model = model
    if args.calibrate:
        logger.info("[%s] recalibrating (Platt scaling) on the %d-row calibration holdout...", args.asset, len(calibration_rows))
        final_model = fit_calibrated(model, calibration_rows, args.horizon_s, args.threshold_bps)

    model_version = f"{args.asset}_{args.horizon_s}s_{args.threshold_bps}bps_{args.model}_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    artifacts_dir = PROJECT_ROOT / "data" / "models"
    artifact_path = save_model(final_model, artifacts_dir / f"{model_version}.joblib")
    logger.info("[%s] saved fitted model to %s", args.asset, artifact_path)

    registry = ModelRegistry(PROJECT_ROOT / "data" / "models" / "registry.jsonl")
    registry.register(
        model_version=model_version,
        asset=args.asset,
        horizon_s=args.horizon_s,
        threshold_bps=args.threshold_bps,
        model_kind=f"{args.model}{'+calibrated' if args.calibrate else ''}",
        training_period={
            "start": args.start, "end": args.end,
            "n_rows_total": len(rows), "n_train_rows": len(train_rows), "n_calibration_rows": len(calibration_rows),
            "cv_folds": args.cv_folds, "n_cv_folds_used": cv_result["n_folds_used"], "n_cv_folds_skipped": cv_result["n_folds_skipped"],
        },
        features=model.feature_columns_,
        parameters={"C": getattr(model, "C", None)} if args.model == "logistic" else {},
        performance={
            "directional_accuracy": accuracy,
            "calibration_diagnostics": {k: v for k, v in diagnostics.items() if k != "calibration_curve"},
            "event_level_accuracy": event_level,
            "net_expected_value": ev_result if args.round_trip_cost_bps is not None else None,
        },
        calibration=diagnostics,
        notes=args.notes,
        # Relative to `artifacts_dir` (== data/models, the exact root
        # `src/live/trading_loop.py`'s `load_promoted_model()` joins
        # `artifact_path` against as `model_registry_root / artifact_path`
        # -- NOT relative to PROJECT_ROOT. Storing it PROJECT_ROOT-relative
        # (the bug this replaces) made `load_promoted_model()` look for
        # `data/models/data/models/<file>.joblib`, which never exists;
        # `.as_posix()` keeps the stored path portable if a model is
        # trained on Windows and the registry is later read on Linux (a
        # `\`-separated path is just a literal filename character on
        # POSIX, not a separator, so it wouldn't have resolved either).
        artifact_path=artifact_path.relative_to(artifacts_dir).as_posix(),
    )
    logger.info("[%s] registered model_version=%s", args.asset, model_version)

    if args.promote:
        registry.promote(model_version, asset=args.asset, horizon_s=args.horizon_s, threshold_bps=args.threshold_bps, reason=args.reason)
        logger.info("[%s] promoted model_version=%s: %s", args.asset, model_version, args.reason)
    else:
        logger.info(
            "[%s] NOT promoted (pass --promote --reason \"...\" to make this the live paper-trading model for "
            "asset=%s horizon_s=%s threshold_bps=%s)", args.asset, args.asset, args.horizon_s, args.threshold_bps,
        )


if __name__ == "__main__":
    main()
