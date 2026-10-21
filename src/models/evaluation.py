"""Minimal directional-accuracy check for the section-31 baselines: does
a baseline's *sign* beat a coin flip at predicting the sign of the
forward move, on an already-built Phase 7 feature+target table?

Roadmap: PHASE 8. New file — Phase 1 didn't stub this (only
`baseline_model.py` was reserved for Phase 8); it exists to give Phase 8
a testable answer to "beats a flip of a coin" (docs/09_tests_plan.md's
placeholder for this phase) without reaching into Phase 9/10 territory:

- No probability calibration, Brier score, or ROC-AUC — that's
  `models/calibration.py`, Phase 9, and only meaningful for a model that
  outputs a probability. A baseline outputs a raw signed score, not a
  probability.
- No PnL, fees, slippage, Sharpe, walk-forward, or bootstrap CI — that's
  `backtest/metrics.py` / `backtest/walk_forward.py`, Phase 10. This
  module answers "is the *direction* better than random", nothing about
  whether trading it would have been profitable after costs.

This is intentionally the simplest test that's still statistically
honest: a one-sided exact binomial test against p=0.5, not just "is
accuracy > 50%" (which a small, noisy sample can satisfy by chance —
`docs/08_backtesting_strategy.md`'s bar is "beats baselines", and a
baseline that hasn't itself cleared basic statistical significance
against a coin flip isn't a bar worth clearing).
"""
from __future__ import annotations

from scipy.stats import binomtest

from src.models.baseline_model import baseline_signal

DEFAULT_SIGNIFICANCE_LEVEL = 0.05


def actual_direction(row: dict, horizon_s: int, threshold_bps: float) -> int | None:
    """+1 / -1 / 0 (flat — the move didn't clear the threshold either way)
    from Phase 7's `compute_targets()` output columns, or `None` if the
    target wasn't computable (insufficient forward data at the end of the
    replayed period). Public (Phase 9 addition — was private when this
    module only had one caller): `models/feature_matrix.py` reuses this
    exact function for label extraction, so the baselines' "beats a coin
    flip" check and the statistical models' training labels can never
    silently diverge on what "up"/"down"/"flat" means for the same
    horizon/threshold pair."""
    up = row.get(f"target_{horizon_s}s_up_{threshold_bps}bps")
    down = row.get(f"target_{horizon_s}s_down_{threshold_bps}bps")
    if up is None or down is None:
        return None
    if up and down:
        # Can't happen for threshold_bps > 0 (up needs >= +b, down needs
        # <= -b) — guarded defensively rather than silently picking one.
        raise ValueError("row has both up and down targets set — inconsistent target columns")
    if up:
        return 1
    if down:
        return -1
    return 0


def evaluate_direction_accuracy(
    rows: "list[dict]",
    kind: str,
    horizon_s: int,
    threshold_bps: float,
    *,
    baseline_kwargs: "dict | None" = None,
    significance_level: float = DEFAULT_SIGNIFICANCE_LEVEL,
) -> dict:
    """Runs `baseline_signal(row, kind, **baseline_kwargs)` over every row,
    keeps only rows where the baseline made a directional call
    (`signal != 0`) *and* the target resolved to a clear up/down move
    (excludes flat-target rows, same convention any directional-accuracy
    study uses — there's no "right answer" to score a flat move against),
    and reports whether the baseline's sign matched the actual direction
    significantly more often than chance.

    Returns a dict: `n_rows` (input size), `n_scored` (directional calls
    with a conclusive target), `n_correct`, `accuracy`, `p_value`
    (one-sided exact binomial test against p=0.5 — `None` if `n_scored`
    is 0), `beats_coin_flip` (`p_value is not None and p_value <
    significance_level` — `False`, not `None`, when there's nothing to
    score, since "no evidence found" is the honest read of an empty
    sample, not a claim of success)."""
    kwargs = baseline_kwargs or {}
    n_correct = 0
    n_scored = 0

    for row in rows:
        actual = actual_direction(row, horizon_s, threshold_bps)
        if actual is None or actual == 0:
            continue
        signal = baseline_signal(row, kind, **kwargs)
        if signal == 0:
            continue
        n_scored += 1
        predicted = 1 if signal > 0 else -1
        if predicted == actual:
            n_correct += 1

    accuracy = (n_correct / n_scored) if n_scored else None
    p_value = binomtest(n_correct, n_scored, p=0.5, alternative="greater").pvalue if n_scored else None

    return {
        "kind": kind,
        "horizon_s": horizon_s,
        "threshold_bps": threshold_bps,
        "n_rows": len(rows),
        "n_scored": n_scored,
        "n_correct": n_correct,
        "accuracy": accuracy,
        "p_value": p_value,
        "beats_coin_flip": bool(p_value is not None and p_value < significance_level),
    }


def evaluate_all_baselines(
    rows: "list[dict]",
    horizon_s: int,
    threshold_bps: float,
    *,
    baseline_kwargs: "dict | None" = None,
    significance_level: float = DEFAULT_SIGNIFICANCE_LEVEL,
) -> "list[dict]":
    """`evaluate_direction_accuracy()` for every kind in
    `baseline_model.BASELINE_KINDS`, for convenience when scanning all 7
    at once (e.g. from a notebook)."""
    from src.models.baseline_model import BASELINE_KINDS

    return [
        evaluate_direction_accuracy(
            rows, kind, horizon_s, threshold_bps,
            baseline_kwargs=baseline_kwargs, significance_level=significance_level,
        )
        for kind in BASELINE_KINDS
    ]
