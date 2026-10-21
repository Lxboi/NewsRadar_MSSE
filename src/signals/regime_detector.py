"""Volatility/liquidity regime detection: LOW_VOL / NORMAL_VOL /
HIGH_VOL / EXTREME_VOL (section 19). Can veto entries in some regimes
(`config/strategy.yaml: entry_filter.allowed_regimes`).

Roadmap: PHASE 7.

Thresholds are relative to each asset's own recent realized-volatility
history rather than a hardcoded per-asset constant — same convention
`orderbook/liquidity.py` uses for its wall-size threshold (section 4:
never assume a fixed default is "correct" just because it runs). "High
volatility" means something very different for BTC than for a newly
added, thinner asset, so the boundaries are quantiles of a rolling
history, not a fixed bps number.
"""
from __future__ import annotations

from collections import deque

DEFAULT_HISTORY_MAXLEN = 500
# Below this many observations, quantile boundaries would just be noise —
# classify as NORMAL_VOL and keep accumulating history instead (same
# cold-start pattern as trade_flow.py's large_trade_min_samples).
DEFAULT_MIN_SAMPLES = 30
DEFAULT_LOW_QUANTILE = 0.25
DEFAULT_HIGH_QUANTILE = 0.75
DEFAULT_EXTREME_QUANTILE = 0.95


def detect_regime(
    volatility: float | None,
    *,
    low_threshold: float,
    high_threshold: float,
    extreme_threshold: float,
) -> str:
    """Pure classification given already-computed thresholds (`low <=
    high <= extreme`, all in the same units as `volatility`). Kept
    separate from `RegimeDetector` so the classification rule itself is
    trivially unit-testable without a rolling-history fixture.

    Bug found via a Phase 7 smoke test (a synthetic book that stopped
    moving after the first couple of ticks): a long run of *exactly*
    identical volatility observations (e.g. 0.0 during a quiet market, or
    any constant value) collapses every quantile boundary to that same
    number. With a naive `>=` chain, the very next identical observation
    then satisfies `volatility >= extreme_threshold` and gets classified
    EXTREME_VOL — the least meaningful possible label for "nothing is
    happening." Guarded narrowly: only when thresholds are fully
    degenerate (no spread at all) *and* the new observation is
    indistinguishable from that flat history (exactly equal to it) does
    this resolve to NORMAL_VOL. A genuinely different observation against
    a degenerate history (e.g. one real outlier after a long flat run)
    still classifies normally below — there the flat history is exactly
    what makes the deviation meaningful, not a reason to suppress it.
    """
    if volatility is None:
        return "NORMAL_VOL"
    if low_threshold == high_threshold == extreme_threshold and volatility == extreme_threshold:
        return "NORMAL_VOL"
    if volatility >= extreme_threshold:
        return "EXTREME_VOL"
    if volatility >= high_threshold:
        return "HIGH_VOL"
    if volatility <= low_threshold:
        return "LOW_VOL"
    return "NORMAL_VOL"


class RegimeDetector:
    """One instance per asset. Call `update(volatility)` once per feature
    row, in chronological order — it classifies the *current* observation
    against quantile boundaries computed from history collected so far
    (never including the point being classified), then appends it to
    history for future calls. Because thresholds only ever look backward,
    this stays a valid feature under section 29 rule 3 (backward-looking
    windows only) even though it's adaptive rather than fixed."""

    def __init__(
        self,
        history_maxlen: int = DEFAULT_HISTORY_MAXLEN,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        low_quantile: float = DEFAULT_LOW_QUANTILE,
        high_quantile: float = DEFAULT_HIGH_QUANTILE,
        extreme_quantile: float = DEFAULT_EXTREME_QUANTILE,
    ) -> None:
        if not (0 <= low_quantile <= high_quantile <= extreme_quantile <= 1):
            raise ValueError("quantiles must satisfy 0 <= low <= high <= extreme <= 1")
        self.history: deque[float] = deque(maxlen=history_maxlen)
        self.min_samples = min_samples
        self.low_quantile = low_quantile
        self.high_quantile = high_quantile
        self.extreme_quantile = extreme_quantile

    def update(self, volatility: float | None) -> str:
        if volatility is None:
            return "NORMAL_VOL"
        if len(self.history) < self.min_samples:
            self.history.append(volatility)
            return "NORMAL_VOL"
        thresholds = self._thresholds()
        regime = detect_regime(volatility, **thresholds)
        self.history.append(volatility)
        return regime

    def _thresholds(self) -> dict:
        ordered = sorted(self.history)
        return {
            "low_threshold": _quantile(ordered, self.low_quantile),
            "high_threshold": _quantile(ordered, self.high_quantile),
            "extreme_threshold": _quantile(ordered, self.extreme_quantile),
        }


def _quantile(sorted_values: "list[float]", q: float) -> float:
    """Nearest-rank quantile — no interpolation, so the result is always
    an actual observed value from history (deliberately simple; this
    isn't a statistical-modeling module, just a threshold picker)."""
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
    return sorted_values[idx]
