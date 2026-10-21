"""Phase 11 tests: models/direction.py — the shared
`direction_from_probabilities()` primitive extracted so
backtest/event_backtester.py, signals/signal_engine.py,
signals/scoring.py, and strategy/exit.py never independently decide
"which direction did the model pick" in divergent ways.
"""
from __future__ import annotations

import pytest

from src.models.direction import CLASS_ORDER, direction_from_probabilities


def test_up_wins_when_strictly_highest():
    direction, probability = direction_from_probabilities(
        {"probability_up": 0.7, "probability_down": 0.2, "probability_neutral": 0.1}
    )
    assert direction == 1
    assert probability == 0.7


def test_down_wins_when_strictly_highest():
    direction, probability = direction_from_probabilities(
        {"probability_up": 0.1, "probability_down": 0.75, "probability_neutral": 0.15}
    )
    assert direction == -1
    assert probability == 0.75


def test_neutral_wins_when_strictly_highest():
    direction, probability = direction_from_probabilities(
        {"probability_up": 0.2, "probability_down": 0.2, "probability_neutral": 0.6}
    )
    assert direction == 0
    assert probability == 0.6


@pytest.mark.parametrize(
    "proba,expected_direction",
    [
        # Exact tie between all three -> up wins (CLASS_ORDER: up > down > neutral).
        ({"probability_up": 1 / 3, "probability_down": 1 / 3, "probability_neutral": 1 / 3}, 1),
        # Tie between down and neutral only -> down wins.
        ({"probability_up": 0.2, "probability_down": 0.4, "probability_neutral": 0.4}, -1),
        # Tie between up and down only -> up wins.
        ({"probability_up": 0.45, "probability_down": 0.45, "probability_neutral": 0.10}, 1),
    ],
)
def test_ties_break_in_class_order(proba, expected_direction):
    direction, _ = direction_from_probabilities(proba)
    assert direction == expected_direction


def test_class_order_constant_matches_tie_break_priority():
    # up(1) > down(-1) > neutral(0), documented tie-break order.
    assert CLASS_ORDER == (1, -1, 0)


def test_missing_key_raises_keyerror_rather_than_silently_defaulting():
    # A caller building `proba` incompletely should fail loudly, not have
    # a missing class silently treated as probability 0 by accident.
    with pytest.raises(KeyError):
        direction_from_probabilities({"probability_up": 0.9, "probability_down": 0.1})
