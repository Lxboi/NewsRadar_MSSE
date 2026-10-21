"""Phase 15 tests: models/model_persistence.py — save_model()/load_model()
round-trip for the fitted model objects models/model_registry.py's
`artifact_path` points at."""
from __future__ import annotations

import numpy as np
import pytest

from src.models.logistic_model import LogisticDirectionModel
from src.models.model_persistence import load_model, save_model
from src.models.tree_model import TreeDirectionModel


def make_rows(n=60, seed=0):
    rng = np.random.RandomState(seed)
    rows = []
    for i in range(n):
        up = i % 2 == 0
        rows.append({
            "asset": "BTC",
            "obi_10": rng.uniform(0.3, 1.0) if up else rng.uniform(-1.0, -0.3),
            "market_regime": "NORMAL_VOL",
            "target_30s_up_10bps": 1 if up else 0,
            "target_30s_down_10bps": 0 if up else 1,
        })
    return rows


def test_save_and_load_round_trip_preserves_predictions(tmp_path):
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0)
    model.fit(make_rows())
    rows = make_rows(n=10, seed=1)
    before = model.predict_proba(rows)

    path = save_model(model, tmp_path / "models" / "btc_30s_10bps.joblib")
    assert path.exists()

    loaded = load_model(path)
    after = loaded.predict_proba(rows)
    assert after == before


def test_save_model_creates_parent_directories(tmp_path):
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0)
    model.fit(make_rows())
    target = tmp_path / "a" / "b" / "c" / "model.joblib"
    assert not target.parent.exists()
    save_model(model, target)
    assert target.exists()


def test_load_model_raises_file_not_found_for_missing_artifact(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_model(tmp_path / "does_not_exist.joblib")


def test_load_model_checks_expected_class(tmp_path):
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0)
    model.fit(make_rows())
    path = save_model(model, tmp_path / "model.joblib")

    # Loading with the correct expected_class works.
    loaded = load_model(path, expected_class=LogisticDirectionModel)
    assert isinstance(loaded, LogisticDirectionModel)

    # Loading with the wrong expected_class raises a clear TypeError.
    with pytest.raises(TypeError, match="TreeDirectionModel"):
        load_model(path, expected_class=TreeDirectionModel)


def test_tree_model_round_trip_too(tmp_path):
    # Not just the logistic model — the persistence layer is generic
    # (plain joblib.dump/load), so it must work for any fitted model
    # class this codebase produces.
    model = TreeDirectionModel(horizon_s=30, threshold_bps=10, random_state=0)
    model.fit(make_rows())
    rows = make_rows(n=10, seed=2)
    before = model.predict_proba(rows)

    path = save_model(model, tmp_path / "tree_model.joblib")
    loaded = load_model(path, expected_class=TreeDirectionModel)
    after = loaded.predict_proba(rows)
    assert after == before
