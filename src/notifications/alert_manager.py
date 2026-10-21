"""Anti-spam layer: cooldown, deduplication, event escalation rules
before anything reaches telegram_bot (section 50).

Roadmap: PHASE 12. `AlertManager.should_notify(alert, *, now=None) -> bool`
is a pure decision function — same convention as
`strategy/entry.py: should_enter()`/`strategy/exit.py: should_exit()`:
given the current internal state (cooldown clocks, last-alerted
probabilities) plus an `alert` dict, it decides, but never mutates. A
caller that decides to actually send must call `record_sent()`
afterwards to update the cooldown/dedup state — kept as an explicit
second step rather than a side effect of `should_notify()` returning
`True`, since a caller might reasonably check "would this be sent" for
diagnostic purposes (e.g. a `/status`-style preview) without actually
sending it.

## Structural enforcement of section 51/52's closed lists

`alert["type"]` must be one of section 51's 5 `ALERT_TYPES` — anything
else raises `ValueError`. This is the same "structural, not conventional"
enforcement `backtest/orderbook_backtester.py` (Phase 10) used for its
news-column ablation: section 52's `never_notify` list (every news item,
every OBI tick, every C-signal, ...) is satisfied by construction, since
nothing outside the 5 allowed types can even reach this gate — not by
this module remembering to filter those categories out.

## `signal_filter` (config/telegram.yaml) vs `entry_filter` (strategy.yaml)

Both gate on probability/expected-value/spread/data-age, but they answer
different questions and are **deliberately more conservative here**:
`strategy/entry.py`'s `entry_filter` asks "is this worth trading" (a
signal quality-C, ~55%-probability edge can still clear it); this
module's `signal_filter` asks "is this worth interrupting the operator
for" — hence `min_probability=0.68`/`required_signal_quality="A"`,
tighter than trading's own `0.62`. A signal can legitimately be traded
without ever generating a notification.

## A known config inconsistency, flagged rather than silently resolved

`config/telegram.yaml: modes.AGGRESSIVE.notify_on_quality` allows
`["A", "B"]`, but `signal_filter.required_signal_quality: "A"` is
documented as applying "ALL must pass, or NO NOTIFICATION" — i.e.
unconditionally. Applied literally (as this module does), AGGRESSIVE
mode's stated "B" allowance is currently unreachable, since the global
filter still demands exactly `"A"`. This isn't resolved here by
guessing which value was meant to change — implemented exactly as
written, both defaults left alone, and flagged in
`docs/17_risk_and_telegram.md` for the user to reconcile intentionally.

## "Exceptional edge" (SNIPER mode) — an interpretive choice

`config/telegram.yaml: modes.SNIPER.require_exceptional_edge: true` is
not otherwise defined by the master prompt. Interpreted here as a
further-tightened probability floor specific to SNIPER mode
(`sniper_exceptional_probability_min`, default `0.75` — stricter than
`signal_filter.min_probability`'s `0.68`) layered on top of every other
`signal_filter` check, not a replacement for them. Flagged as an
interpretive choice, not a spec-given number, same as every other
un-tuned default in this codebase.
"""
from __future__ import annotations

import datetime as dt

from src.models.direction import direction_from_probabilities

ALERT_TYPES = (
    "HIGH_CONVICTION_SIGNAL",
    "POSITION_OPENED",
    "POSITION_CLOSED",
    "RISK_ALERT",
    "DAILY_REPORT",
)

ESCALATE_ON_REASONS = (
    "signal_quality_change",
    "significant_probability_change",
    "direction_change",
    "new_major_news",
    "position_opened",
    "position_closed",
    "risk_event",
)

MODES = ("AGGRESSIVE", "NORMAL", "SNIPER")
DEFAULT_MODE = "SNIPER"
MODE_NOTIFY_ON_QUALITY = {
    "AGGRESSIVE": ("A", "B"),
    "NORMAL": ("A",),
    "SNIPER": ("A",),
}

DEFAULT_COOLDOWN_S_PER_ASSET = 300             # telegram.yaml: anti_spam.cooldown_s_per_asset
DEFAULT_COOLDOWN_OVERRIDE_ON_MAJOR_EVENT = True
DEFAULT_DEDUPE_PROBABILITY_DELTA_PCT = 3.0     # telegram.yaml: anti_spam.dedupe_probability_delta_pct
DEFAULT_BLOCK_ON_RECENT_DUPLICATE = True
DEFAULT_BLOCK_ON_RISK_LOCK = True

DEFAULT_MIN_PROBABILITY = 0.68                 # telegram.yaml: signal_filter.min_probability
DEFAULT_MIN_EXPECTED_VALUE = 0.0               # telegram.yaml: signal_filter.min_expected_value
DEFAULT_REQUIRED_SIGNAL_QUALITY = "A"          # telegram.yaml: signal_filter.required_signal_quality
DEFAULT_MAX_SPREAD_BPS = 5                     # telegram.yaml: signal_filter.max_spread_bps
DEFAULT_MAX_DATA_AGE_S = 3                     # telegram.yaml: signal_filter.max_data_age_s
DEFAULT_SNIPER_EXCEPTIONAL_PROBABILITY_MIN = 0.75  # interpretive, see module docstring


class AlertManager:
    def __init__(
        self,
        *,
        mode: str = DEFAULT_MODE,
        cooldown_s_per_asset: float = DEFAULT_COOLDOWN_S_PER_ASSET,
        cooldown_override_on_major_event: bool = DEFAULT_COOLDOWN_OVERRIDE_ON_MAJOR_EVENT,
        dedupe_probability_delta_pct: float = DEFAULT_DEDUPE_PROBABILITY_DELTA_PCT,
        block_on_recent_duplicate: bool = DEFAULT_BLOCK_ON_RECENT_DUPLICATE,
        block_on_risk_lock: bool = DEFAULT_BLOCK_ON_RISK_LOCK,
        min_probability: float = DEFAULT_MIN_PROBABILITY,
        min_expected_value: float = DEFAULT_MIN_EXPECTED_VALUE,
        required_signal_quality: "str | None" = DEFAULT_REQUIRED_SIGNAL_QUALITY,
        max_spread_bps: float = DEFAULT_MAX_SPREAD_BPS,
        max_data_age_s: float = DEFAULT_MAX_DATA_AGE_S,
        sniper_exceptional_probability_min: float = DEFAULT_SNIPER_EXCEPTIONAL_PROBABILITY_MIN,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode: {mode!r}")
        self.mode = mode
        self.cooldown_s_per_asset = cooldown_s_per_asset
        self.cooldown_override_on_major_event = cooldown_override_on_major_event
        self.dedupe_probability_delta_pct = dedupe_probability_delta_pct
        self.block_on_recent_duplicate = block_on_recent_duplicate
        self.block_on_risk_lock = block_on_risk_lock
        self.min_probability = min_probability
        self.min_expected_value = min_expected_value
        self.required_signal_quality = required_signal_quality
        self.max_spread_bps = max_spread_bps
        self.max_data_age_s = max_data_age_s
        self.sniper_exceptional_probability_min = sniper_exceptional_probability_min

        self._last_sent_at: "dict[tuple[str, str], dt.datetime]" = {}
        self._last_sent_probability: "dict[str, float]" = {}

    def should_notify(self, alert: dict, *, now: "dt.datetime | None" = None) -> bool:
        alert_type = alert.get("type")
        if alert_type not in ALERT_TYPES:
            raise ValueError(f"unknown alert type: {alert_type!r}")

        escalation_reason = alert.get("escalation_reason")
        if escalation_reason is not None and escalation_reason not in ESCALATE_ON_REASONS:
            raise ValueError(f"unknown escalation reason: {escalation_reason!r}")
        is_escalated = escalation_reason is not None

        if alert_type == "HIGH_CONVICTION_SIGNAL" and not self._passes_signal_filter(alert, now=now):
            return False

        asset = alert.get("asset")
        probability = self._alert_probability(alert)

        if self.block_on_recent_duplicate and asset is not None and probability is not None:
            last_probability = self._last_sent_probability.get(asset)
            if last_probability is not None:
                delta_pct = abs(probability - last_probability) * 100.0
                if delta_pct < self.dedupe_probability_delta_pct:
                    return False

        if asset is not None and now is not None:
            last_at = self._last_sent_at.get((asset, alert_type))
            if last_at is not None:
                elapsed_s = (now - last_at).total_seconds()
                in_cooldown = elapsed_s < self.cooldown_s_per_asset
                overridden = is_escalated and self.cooldown_override_on_major_event
                if in_cooldown and not overridden:
                    return False

        return True

    def record_sent(self, alert: dict, *, now: "dt.datetime | None" = None) -> None:
        """Call after actually sending `alert` (per `should_notify()`
        returning `True`) — updates the cooldown clock and, for a signal
        alert, the last-alerted probability used for dedup."""
        asset = alert.get("asset")
        alert_type = alert.get("type")
        if asset is not None and now is not None:
            self._last_sent_at[(asset, alert_type)] = now
        probability = self._alert_probability(alert)
        if asset is not None and probability is not None:
            self._last_sent_probability[asset] = probability

    def _alert_probability(self, alert: dict) -> "float | None":
        signal = alert.get("signal")
        if not signal:
            return None
        try:
            _, probability = direction_from_probabilities(signal)
        except KeyError:
            return None
        return probability

    def _passes_signal_filter(self, alert: dict, *, now: "dt.datetime | None") -> bool:
        if alert.get("risk_blocked") and self.block_on_risk_lock:
            return False

        signal = alert.get("signal")
        if not signal:
            return False

        try:
            direction, probability = direction_from_probabilities(signal)
        except KeyError:
            return False
        if direction == 0:
            return False

        quality = signal.get("signal_quality")
        if quality not in MODE_NOTIFY_ON_QUALITY.get(self.mode, ()):
            return False
        if self.required_signal_quality is not None and quality != self.required_signal_quality:
            return False

        if self.min_probability is not None and probability < self.min_probability:
            return False

        expected_value = signal.get("expected_value")
        if expected_value is not None and self.min_expected_value is not None and expected_value < self.min_expected_value:
            return False

        features = signal.get("features_snapshot") or {}
        spread = features.get("spread")
        if spread is not None and self.max_spread_bps is not None and spread > self.max_spread_bps:
            return False

        if self.mode == "SNIPER" and probability < self.sniper_exceptional_probability_min:
            return False

        timestamp = signal.get("timestamp")
        if now is not None and timestamp is not None and self.max_data_age_s is not None:
            age_s = (now - timestamp).total_seconds()
            if age_s > self.max_data_age_s:
                return False

        return True
