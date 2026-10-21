"""Phase 5 test: decay function matches the closed-form formula at known
ages (section 25). See docs/09_tests_plan.md."""
import datetime as dt
import math

import pytest

from src.news.event_detector import apply_decay


def test_apply_decay_at_zero_age_returns_full_impact():
    now = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
    event = {"ingestion_time": now, "initial_impact": 0.8, "half_life_s": 300}
    result = apply_decay(event, now)
    assert result["age_s"] == 0.0
    assert result["current_impact"] == pytest.approx(0.8)


def test_apply_decay_at_one_half_life_halves_impact():
    ingestion = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
    now = ingestion + dt.timedelta(seconds=300)
    event = {"ingestion_time": ingestion, "initial_impact": 1.0, "half_life_s": 300}
    result = apply_decay(event, now)
    assert result["current_impact"] == pytest.approx(0.5)


def test_apply_decay_matches_closed_form_at_arbitrary_age():
    ingestion = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
    age_s = 123.0
    half_life_s = 300.0
    now = ingestion + dt.timedelta(seconds=age_s)
    initial_impact = 0.6
    event = {"ingestion_time": ingestion, "initial_impact": initial_impact, "half_life_s": half_life_s}

    result = apply_decay(event, now)

    expected = initial_impact * math.exp(-(math.log(2) / half_life_s) * age_s)
    assert result["current_impact"] == pytest.approx(expected)
    assert result["age_s"] == pytest.approx(age_s)


def test_apply_decay_does_not_mutate_input():
    now = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
    event = {"ingestion_time": now, "initial_impact": 0.5, "half_life_s": 300}
    apply_decay(event, now)
    assert "current_impact" not in event


def test_apply_decay_defaults_when_fields_missing():
    now = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
    result = apply_decay({"ingestion_time": now}, now)
    assert result["current_impact"] == 0.0
