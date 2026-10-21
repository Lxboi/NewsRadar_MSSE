"""Phase 8 tests: models/evaluation.py — the "beats a flip of a coin"
directional-accuracy check docs/09_tests_plan.md promised for this phase.
Pure computation over hand-built synthetic feature+target rows, no I/O,
no network.

The two core tests here are deliberately constructed (not randomly
sampled) so they're non-flaky: one dataset where the baseline signal is
*perfectly* correlated with the actual direction (must report high
accuracy and reject the coin-flip null), and one where it's exactly a
50/50 coin flip by construction (must NOT reject the null) — the second
is the one that actually matters: a test suite that can only say "yes,
significant" and never "no, not significant" isn't testing anything.
"""
import pytest

from src.models.baseline_model import BASELINE_KINDS
from src.models.evaluation import evaluate_all_baselines, evaluate_direction_accuracy


def row(signal, direction, *, horizon_s=30, threshold_bps=10):
    """One synthetic feature+target row: `signal` becomes the row's
    `momentum` baseline input (`returns_{horizon_s}s`); `direction` is
    +1 (target up), -1 (target down), or 0 (flat — neither target set)."""
    r = {f"returns_{horizon_s}s": signal}
    r[f"target_{horizon_s}s_up_{threshold_bps}bps"] = 1 if direction == 1 else 0
    r[f"target_{horizon_s}s_down_{threshold_bps}bps"] = 1 if direction == -1 else 0
    return r


def test_perfectly_correlated_baseline_beats_coin_flip():
    rows = [row(0.01, 1) for _ in range(30)] + [row(-0.01, -1) for _ in range(30)]

    result = evaluate_direction_accuracy(rows, "momentum", horizon_s=30, threshold_bps=10)

    assert result["n_scored"] == 60
    assert result["n_correct"] == 60
    assert result["accuracy"] == pytest.approx(1.0)
    assert result["p_value"] < 0.001
    assert result["beats_coin_flip"] is True


def test_exact_coin_flip_does_not_beat_coin_flip():
    # 50 correct, 50 wrong, by construction — literally the null hypothesis.
    rows = [row(0.01, 1) for _ in range(50)] + [row(0.01, -1) for _ in range(50)]

    result = evaluate_direction_accuracy(rows, "momentum", horizon_s=30, threshold_bps=10)

    assert result["n_scored"] == 100
    assert result["n_correct"] == 50
    assert result["accuracy"] == pytest.approx(0.5)
    assert result["p_value"] > 0.05
    assert result["beats_coin_flip"] is False


def test_flat_target_rows_are_excluded_from_scoring():
    rows = [row(0.01, 1) for _ in range(10)] + [row(0.01, 0) for _ in range(10)]

    result = evaluate_direction_accuracy(rows, "momentum", horizon_s=30, threshold_bps=10)

    assert result["n_rows"] == 20
    assert result["n_scored"] == 10  # the 10 flat-target rows excluded


def test_zero_signal_rows_are_excluded_from_scoring():
    rows = [row(0.01, 1) for _ in range(10)] + [row(0.0, 1) for _ in range(10)]

    result = evaluate_direction_accuracy(rows, "momentum", horizon_s=30, threshold_bps=10)

    assert result["n_scored"] == 10  # the 10 zero-signal rows excluded


def test_missing_target_columns_excluded_not_crashed():
    rows = [{"returns_30s": 0.01}]  # no target_* keys at all

    result = evaluate_direction_accuracy(rows, "momentum", horizon_s=30, threshold_bps=10)

    assert result["n_scored"] == 0
    assert result["accuracy"] is None
    assert result["p_value"] is None
    assert result["beats_coin_flip"] is False


def test_both_up_and_down_targets_set_raises():
    bad_row = {
        "returns_30s": 0.01,
        "target_30s_up_10bps": 1,
        "target_30s_down_10bps": 1,
    }
    with pytest.raises(ValueError):
        evaluate_direction_accuracy([bad_row], "momentum", horizon_s=30, threshold_bps=10)


def test_evaluate_all_baselines_covers_every_kind_in_order():
    rows = [row(0.01, 1) for _ in range(5)]
    results = evaluate_all_baselines(rows, horizon_s=30, threshold_bps=10)
    assert [r["kind"] for r in results] == list(BASELINE_KINDS)
