"""Phase 7 tests: signals/regime_detector.py. Pure computation, no I/O.
See docs/09_tests_plan.md."""
import pytest

from src.signals.regime_detector import RegimeDetector, detect_regime


def test_detect_regime_pure_classification():
    assert detect_regime(0.001, low_threshold=0.01, high_threshold=0.05, extreme_threshold=0.1) == "LOW_VOL"
    assert detect_regime(0.02, low_threshold=0.01, high_threshold=0.05, extreme_threshold=0.1) == "NORMAL_VOL"
    assert detect_regime(0.06, low_threshold=0.01, high_threshold=0.05, extreme_threshold=0.1) == "HIGH_VOL"
    assert detect_regime(0.2, low_threshold=0.01, high_threshold=0.05, extreme_threshold=0.1) == "EXTREME_VOL"


def test_detect_regime_none_volatility_is_normal():
    assert detect_regime(None, low_threshold=0.01, high_threshold=0.05, extreme_threshold=0.1) == "NORMAL_VOL"


def test_regime_detector_rejects_invalid_quantile_ordering():
    with pytest.raises(ValueError):
        RegimeDetector(low_quantile=0.9, high_quantile=0.1, extreme_quantile=0.95)


def test_regime_detector_stays_normal_during_cold_start():
    rd = RegimeDetector(min_samples=10)
    for i in range(9):
        assert rd.update(0.01 * (i + 1)) == "NORMAL_VOL"


def test_regime_detector_classifies_extreme_after_warm_up():
    rd = RegimeDetector(min_samples=10, low_quantile=0.25, high_quantile=0.75, extreme_quantile=0.95)
    # Warm up with a tight, stable volatility history...
    for _ in range(30):
        rd.update(0.01)
    # ...then a wild outlier should register as extreme.
    regime = rd.update(5.0)
    assert regime == "EXTREME_VOL"


def test_regime_detector_none_volatility_returns_normal_without_recording():
    rd = RegimeDetector(min_samples=2)
    assert rd.update(None) == "NORMAL_VOL"
    assert len(rd.history) == 0


def test_detect_regime_degenerate_thresholds_stay_normal_not_extreme():
    """Regression test for a real bug found via a Phase 7 smoke test: a
    long run of identical volatility observations (e.g. a quiet market
    sitting at 0.0) collapses low/high/extreme thresholds to the same
    value. A naive >= chain would then flag every subsequent identical
    observation as EXTREME_VOL — exactly backwards for a flat market."""
    assert detect_regime(0.0, low_threshold=0.0, high_threshold=0.0, extreme_threshold=0.0) == "NORMAL_VOL"
    assert detect_regime(5.0, low_threshold=5.0, high_threshold=5.0, extreme_threshold=5.0) == "NORMAL_VOL"


def test_regime_detector_stays_normal_through_a_long_flat_volatility_run():
    """End-to-end version of the degenerate-threshold bug above, through
    the real RegimeDetector.update() path rather than calling
    detect_regime() directly."""
    rd = RegimeDetector(min_samples=10)
    regimes = [rd.update(0.0) for _ in range(200)]
    assert all(r == "NORMAL_VOL" for r in regimes)


def test_regime_detector_current_observation_never_included_in_its_own_thresholds():
    """The point being classified must be judged against history collected
    strictly before it — never against a threshold set that already
    includes itself (which could make a genuine outlier drag its own
    boundary out just far enough to no longer look extreme)."""
    rd = RegimeDetector(min_samples=5, low_quantile=0.0, high_quantile=0.5, extreme_quantile=1.0)
    for _ in range(5):
        rd.update(0.01)
    # A huge jump: if it were included in its own extreme_threshold
    # (the max of the set), it could never classify as >= the max of a
    # set containing itself unless ties count — asserting EXTREME_VOL
    # confirms the threshold was computed from prior history only.
    assert rd.update(100.0) == "EXTREME_VOL"
