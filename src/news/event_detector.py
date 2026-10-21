"""Applies the exponential decay model impact(t) = initial_impact *
exp(-lambda * age) with configurable half-life (section 25), and hands
off confirmed events to the feature engine (Phase 7).

Look-ahead-bias note (docs/06_features_v1.md flags this as a decision Phase
5 must make once and keep consistent): age is measured from
`ingestion_time`, not `event_time`. A source's stated publish time can be
stale, backdated, or simply wrong; `ingestion_time` is what the system
actually observed and is what every downstream no-look-ahead join already
uses (section 29) — using anything else here would reintroduce exactly the
bias that rule exists to prevent.
"""
from __future__ import annotations

import datetime as dt
import math

DEFAULT_HALF_LIFE_S = 300


def apply_decay(event: dict, now: dt.datetime) -> dict:
    """Returns a new dict (input not mutated) with `age_s` and
    `current_impact` added. `event` needs `ingestion_time` (datetime) and,
    optionally, `initial_impact` (float, default 0.0) and `half_life_s`
    (float, default `DEFAULT_HALF_LIFE_S`)."""
    ingestion_time = event["ingestion_time"]
    half_life_s = event.get("half_life_s", DEFAULT_HALF_LIFE_S)
    initial_impact = event.get("initial_impact", 0.0)

    age_s = max(0.0, (now - ingestion_time).total_seconds())
    if half_life_s and half_life_s > 0:
        lam = math.log(2) / half_life_s
        current_impact = initial_impact * math.exp(-lam * age_s)
    else:
        current_impact = initial_impact if age_s == 0 else 0.0

    out = dict(event)
    out["age_s"] = age_s
    out["current_impact"] = current_impact
    return out
