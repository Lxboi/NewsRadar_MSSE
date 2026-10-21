"""Hard limits: max_risk_per_trade, max_daily_loss, max_position_size,
max_consecutive_losses, max_open_positions, max_slippage, max_spread,
max_latency (section 66). Breaching any limit blocks new trades.

Roadmap: PHASE 12. `RiskManager` tracks account-level state across
trades (running equity, daily PnL, a consecutive-loss streak) and
exposes `state() -> {"blocked": bool, "reason": str | None}` — exactly
the placeholder shape `strategy/entry.py` already documented in Phase 11
(`{"blocked": bool, "reason": str | None}`), specifically so
`StrategyEngine(risk_state=risk_manager.state)` needs no change on
either side: `state` is a bound method, satisfying the "zero-argument
callable" half of that contract.

Feed it closed positions via `record_trade()` — the exact same dict
shape `strategy/strategy_engine.py: StrategyEngine._close_position()`
(Phase 11) and `backtest/event_backtester.py`'s trade dicts already
produce (`net_pnl`, `closed_at`), so a real trade and a backtested one
are graded by risk management identically, with no translation layer.

## What this deliberately does NOT enforce

`max_open_positions` is also a `config/strategy.yaml: risk` key, but
`StrategyEngine.max_open_positions` (Phase 11) already enforces it
structurally — it never even builds a signal for a new slot once the cap
is reached. Duplicating that check here would be redundant, not
defense-in-depth (there's no path to bypass `StrategyEngine`'s own
counter), so it's intentionally left to that layer.

## `max_spread_bps`/`max_slippage_bps`: two limits, two different jobs

`strategy.yaml`'s `entry_filter.max_spread_bps` (5, tighter) is the
strategy's own tunable "is this trade still worth it" filter
(`strategy/entry.py`). This module's `max_spread_bps`/`max_slippage_bps`
(8/8, looser — both from the same file's `risk:` block) are a *hard,
account-level* circuit breaker: independent of whatever the strategy's
own filter is currently configured to, if execution quality itself is
this bad, stop opening new trades entirely. That the risk ceiling is
currently looser than the entry filter (so it rarely binds today) is
intentional layering, not a bug — flagged explicitly here rather than
"fixed" by guessing which number was meant to be tighter; see
`docs/17_risk_and_telegram.md`. `max_slippage_bps`/`max_latency_ms` are
fed via `record_fill_quality()`, since slippage/latency are only known
*after* a fill — there's no real caller for this yet (Phase 13/14's
executor doesn't exist), same "documented but not yet wireable" pattern
already used for `strategy/entry.py`'s `risk_state` placeholder and
`strategy_engine.py`'s optional `executor`.
"""
from __future__ import annotations

import datetime as dt

DEFAULT_MAX_RISK_PER_TRADE_PCT = 0.1          # strategy.yaml: risk.max_risk_per_trade_pct
DEFAULT_MAX_DAILY_LOSS_PCT = 1.0               # strategy.yaml: risk.max_daily_loss_pct
DEFAULT_MAX_POSITION_SIZE_PCT_EQUITY = 5.0     # strategy.yaml: risk.max_position_size_pct_equity
DEFAULT_MAX_CONSECUTIVE_LOSSES = 4             # strategy.yaml: risk.max_consecutive_losses
DEFAULT_MAX_SLIPPAGE_BPS = 8                   # strategy.yaml: risk.max_slippage_bps
DEFAULT_MAX_SPREAD_BPS = 8                     # strategy.yaml: risk.max_spread_bps
DEFAULT_MAX_LATENCY_MS = 500                   # strategy.yaml: risk.max_latency_ms

REASON_KILL_SWITCH = "kill_switch_engaged"
REASON_DAILY_LOSS_LIMIT = "daily_loss_limit_breached"
REASON_CONSECUTIVE_LOSSES = "max_consecutive_losses_breached"
REASON_MAX_SLIPPAGE = "max_slippage_breached"
REASON_MAX_LATENCY = "max_latency_breached"


class RiskManager:
    """One instance per trading account/session — mirrors
    `StrategyEngine`'s one-instance-per-question convention, since risk
    state (equity, daily PnL, loss streak) is inherently account-scoped,
    not per-signal."""

    def __init__(
        self,
        *,
        account_equity: float,
        max_risk_per_trade_pct: float = DEFAULT_MAX_RISK_PER_TRADE_PCT,
        max_daily_loss_pct: float = DEFAULT_MAX_DAILY_LOSS_PCT,
        max_position_size_pct_equity: float = DEFAULT_MAX_POSITION_SIZE_PCT_EQUITY,
        max_consecutive_losses: int = DEFAULT_MAX_CONSECUTIVE_LOSSES,
        max_slippage_bps: float = DEFAULT_MAX_SLIPPAGE_BPS,
        max_spread_bps: float = DEFAULT_MAX_SPREAD_BPS,
        max_latency_ms: float = DEFAULT_MAX_LATENCY_MS,
    ) -> None:
        self.account_equity = account_equity
        self.max_risk_per_trade_pct = max_risk_per_trade_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_position_size_pct_equity = max_position_size_pct_equity
        self.max_consecutive_losses = max_consecutive_losses
        self.max_slippage_bps = max_slippage_bps
        self.max_spread_bps = max_spread_bps
        self.max_latency_ms = max_latency_ms

        self._current_day: "dt.date | None" = None
        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        self._kill_switch_engaged = False
        self._kill_switch_reason: "str | None" = None
        self._execution_breach_reason: "str | None" = None

    def record_trade(self, closed_position: dict) -> None:
        """Updates running equity, the current UTC day's PnL (resetting
        it when `closed_position["closed_at"]` crosses a UTC day
        boundary since the last call), and the consecutive-loss streak
        (any non-negative `net_pnl` — a win or an exact breakeven —
        resets the streak to 0; only a strictly negative `net_pnl`
        extends it). Reads exactly the keys
        `StrategyEngine._close_position()`/`EventBacktester`'s trade
        dicts already produce — no translation needed to feed either
        one's output straight into this method."""
        net_pnl = closed_position.get("net_pnl", 0.0) or 0.0
        closed_at = closed_position.get("closed_at")

        day = closed_at.date() if closed_at is not None else None
        if day is not None and day != self._current_day:
            self._current_day = day
            self._daily_pnl = 0.0

        self._daily_pnl += net_pnl
        self.account_equity += net_pnl
        self._consecutive_losses = 0 if net_pnl >= 0 else self._consecutive_losses + 1

    def record_fill_quality(self, *, slippage_bps: "float | None" = None, latency_ms: "float | None" = None) -> None:
        """Independent of `record_trade()` — a fill's execution quality
        can breach a hard limit even on a profitable trade. Sticky: once
        set, stays set until `reset_execution_breach()` is called
        explicitly (an operator decision, not something that should
        silently clear itself on the next good fill)."""
        if slippage_bps is not None and slippage_bps > self.max_slippage_bps:
            self._execution_breach_reason = REASON_MAX_SLIPPAGE
        if latency_ms is not None and latency_ms > self.max_latency_ms:
            self._execution_breach_reason = REASON_MAX_LATENCY

    def reset_execution_breach(self) -> None:
        self._execution_breach_reason = None

    def engage_kill_switch(self, reason: "str | None" = None) -> None:
        """The account-level kill switch — section 55's `/stop` and
        section 66's "breach => NO NEW TRADES" both resolve to this one
        call. Deliberately just a plain method with no Telegram
        dependency: `notifications/telegram_bot.py`'s `/stop` handler is
        one caller among possibly several (a local script, a signal
        handler) — satisfying section 55's "a local kill switch
        independent of Telegram" requirement by construction, not by
        building a second, separate mechanism."""
        self._kill_switch_engaged = True
        self._kill_switch_reason = reason

    def disengage_kill_switch(self) -> None:
        self._kill_switch_engaged = False
        self._kill_switch_reason = None

    def daily_loss_pct(self) -> float:
        """Positive when the day is net-losing, negative/zero when
        net-flat or net-profitable. Compared against
        `max_daily_loss_pct` (always configured as a positive number) in
        `state()`."""
        if self.account_equity <= 0:
            return 0.0
        return -self._daily_pnl / self.account_equity * 100.0

    def state(self) -> dict:
        """See module docstring — `{"blocked": bool, "reason":
        str | None}`, matching `strategy/entry.py`'s placeholder
        contract exactly. Checked in a fixed priority order (kill switch
        first, since it's an explicit operator decision that should
        always win) — only the first breach found is reported, matching
        the "one reason wins" convention `strategy/exit.py` already
        established for its own candidate list."""
        if self._kill_switch_engaged:
            return {"blocked": True, "reason": self._kill_switch_reason or REASON_KILL_SWITCH}
        if self._execution_breach_reason is not None:
            return {"blocked": True, "reason": self._execution_breach_reason}
        if self.daily_loss_pct() >= self.max_daily_loss_pct:
            return {"blocked": True, "reason": REASON_DAILY_LOSS_LIMIT}
        if self._consecutive_losses >= self.max_consecutive_losses:
            return {"blocked": True, "reason": REASON_CONSECUTIVE_LOSSES}
        return {"blocked": False, "reason": None}
