"""Phase 9 tests: models/model_registry.py — the section-59 audit trail.
Uses pytest's tmp_path fixture for a real (temp) JSONL file per test,
since this module's whole contract is about what persists on disk and
how two independent readers of the same file agree.
"""
import json

import pytest

from src.models.model_registry import ModelRegistry


def _registry(tmp_path, name="registry.jsonl"):
    return ModelRegistry(tmp_path / "nested" / name)


def _register_defaults(reg, model_version="logit-v1", asset="BTC", horizon_s=30, threshold_bps=10, **overrides):
    kwargs = dict(
        model_version=model_version,
        asset=asset,
        horizon_s=horizon_s,
        threshold_bps=threshold_bps,
        model_kind="logistic",
        training_period={"start": "2026-01-01", "end": "2026-02-01"},
        features=["obi", "trade_imbalance"],
        parameters={"C": 1.0},
        performance={"accuracy": 0.6, "p_value": 0.001},
    )
    kwargs.update(overrides)
    return reg.register(**kwargs)


def test_creates_parent_directories_and_file_on_construction(tmp_path):
    path = tmp_path / "a" / "b" / "registry.jsonl"
    assert not path.exists()
    ModelRegistry(path)
    assert path.exists()


def test_register_returns_the_record_and_persists_it_as_jsonl(tmp_path):
    reg = _registry(tmp_path)
    record = _register_defaults(reg)
    assert record["event"] == "register"
    assert record["model_version"] == "logit-v1"

    lines = (tmp_path / "nested" / "registry.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk == record


def test_register_rejects_duplicate_model_version(tmp_path):
    reg = _registry(tmp_path)
    _register_defaults(reg)
    with pytest.raises(ValueError):
        _register_defaults(reg)


def test_list_models_filters_by_asset_horizon_threshold(tmp_path):
    reg = _registry(tmp_path)
    _register_defaults(reg, model_version="btc-30-10")
    _register_defaults(reg, model_version="btc-60-25", horizon_s=60, threshold_bps=25)
    _register_defaults(reg, model_version="eth-30-10", asset="ETH")

    assert {m["model_version"] for m in reg.list_models()} == {"btc-30-10", "btc-60-25", "eth-30-10"}
    assert {m["model_version"] for m in reg.list_models(asset="BTC")} == {"btc-30-10", "btc-60-25"}
    assert {m["model_version"] for m in reg.list_models(asset="BTC", horizon_s=30, threshold_bps=10)} == {"btc-30-10"}
    assert reg.list_models(asset="SOL") == []


def test_promote_rejects_a_never_registered_model_version(tmp_path):
    reg = _registry(tmp_path)
    _register_defaults(reg)
    with pytest.raises(ValueError):
        reg.promote("does-not-exist", asset="BTC", horizon_s=30, threshold_bps=10, reason="test")


def test_promote_rejects_empty_or_whitespace_reason(tmp_path):
    reg = _registry(tmp_path)
    _register_defaults(reg)
    with pytest.raises(ValueError):
        reg.promote("logit-v1", asset="BTC", horizon_s=30, threshold_bps=10, reason="")
    with pytest.raises(ValueError):
        reg.promote("logit-v1", asset="BTC", horizon_s=30, threshold_bps=10, reason="   ")


def test_promote_rejects_cross_key_promotion(tmp_path):
    # Registered for (BTC, 30, 10) — promoting under a different
    # horizon/threshold/asset key must fail, since that combination was
    # never evaluated.
    reg = _registry(tmp_path)
    _register_defaults(reg)
    with pytest.raises(ValueError):
        reg.promote("logit-v1", asset="BTC", horizon_s=60, threshold_bps=10, reason="test")
    with pytest.raises(ValueError):
        reg.promote("logit-v1", asset="ETH", horizon_s=30, threshold_bps=10, reason="test")


def test_current_promoted_is_none_before_any_promotion(tmp_path):
    reg = _registry(tmp_path)
    _register_defaults(reg)
    assert reg.current_promoted(asset="BTC", horizon_s=30, threshold_bps=10) is None


def test_current_promoted_returns_full_register_record(tmp_path):
    reg = _registry(tmp_path)
    _register_defaults(reg)
    reg.promote("logit-v1", asset="BTC", horizon_s=30, threshold_bps=10, reason="best accuracy in backtest")
    current = reg.current_promoted(asset="BTC", horizon_s=30, threshold_bps=10)
    assert current["model_version"] == "logit-v1"
    assert current["performance"] == {"accuracy": 0.6, "p_value": 0.001}


def test_current_promoted_follows_the_most_recent_promotion(tmp_path):
    reg = _registry(tmp_path)
    _register_defaults(reg, model_version="v1")
    _register_defaults(reg, model_version="v2", model_kind="tree")
    reg.promote("v1", asset="BTC", horizon_s=30, threshold_bps=10, reason="first pick")
    reg.promote("v2", asset="BTC", horizon_s=30, threshold_bps=10, reason="v2 generalizes better")
    assert reg.current_promoted(asset="BTC", horizon_s=30, threshold_bps=10)["model_version"] == "v2"


def test_list_promotions_returns_full_history_not_just_latest(tmp_path):
    reg = _registry(tmp_path)
    _register_defaults(reg, model_version="v1")
    _register_defaults(reg, model_version="v2")
    reg.promote("v1", asset="BTC", horizon_s=30, threshold_bps=10, reason="first")
    reg.promote("v2", asset="BTC", horizon_s=30, threshold_bps=10, reason="second")
    history = reg.list_promotions(asset="BTC", horizon_s=30, threshold_bps=10)
    assert [p["model_version"] for p in history] == ["v1", "v2"]
    assert [p["reason"] for p in history] == ["first", "second"]


def test_promotion_never_mutates_the_register_record_append_only(tmp_path):
    # The whole point of an append-only log: promoting must not rewrite
    # or remove the original register() line.
    path = tmp_path / "nested" / "registry.jsonl"
    reg = ModelRegistry(path)
    _register_defaults(reg)
    lines_before = path.read_text().strip().splitlines()
    reg.promote("logit-v1", asset="BTC", horizon_s=30, threshold_bps=10, reason="ok")
    lines_after = path.read_text().strip().splitlines()
    assert lines_after[: len(lines_before)] == lines_before
    assert len(lines_after) == len(lines_before) + 1


def test_two_registry_instances_pointed_at_the_same_file_see_the_same_data(tmp_path):
    path = tmp_path / "nested" / "registry.jsonl"
    reg1 = ModelRegistry(path)
    _register_defaults(reg1)
    reg1.promote("logit-v1", asset="BTC", horizon_s=30, threshold_bps=10, reason="ok")

    reg2 = ModelRegistry(path)  # independent instance, same file
    current = reg2.current_promoted(asset="BTC", horizon_s=30, threshold_bps=10)
    assert current is not None
    assert current["model_version"] == "logit-v1"


def test_register_stores_calibration_and_notes_when_given(tmp_path):
    reg = _registry(tmp_path)
    record = _register_defaults(
        reg,
        calibration={"brier_score": 0.2, "roc_auc_macro": 0.7},
        notes="trained on Jan-Feb synthetic replay data",
    )
    assert record["calibration"] == {"brier_score": 0.2, "roc_auc_macro": 0.7}
    assert record["notes"] == "trained on Jan-Feb synthetic replay data"


def test_register_defaults_calibration_and_notes_to_none_when_omitted(tmp_path):
    reg = _registry(tmp_path)
    record = _register_defaults(reg)
    assert record["calibration"] is None
    assert record["notes"] is None


def test_register_records_artifact_path_when_given(tmp_path):
    # Phase 15: models/model_persistence.py's save_model() destination,
    # optional and backward-compatible (see module docstring).
    reg = _registry(tmp_path)
    record = _register_defaults(reg, artifact_path="models/artifacts/BTC_30s_10bps_logit-v1.joblib")
    assert record["artifact_path"] == "models/artifacts/BTC_30s_10bps_logit-v1.joblib"

    promoted = reg.current_promoted(asset="BTC", horizon_s=30, threshold_bps=10)
    reg.promote("logit-v1", asset="BTC", horizon_s=30, threshold_bps=10, reason="best walk-forward Sharpe")
    promoted = reg.current_promoted(asset="BTC", horizon_s=30, threshold_bps=10)
    assert promoted["artifact_path"] == "models/artifacts/BTC_30s_10bps_logit-v1.joblib"


def test_register_defaults_artifact_path_to_none_when_omitted(tmp_path):
    reg = _registry(tmp_path)
    record = _register_defaults(reg)
    assert record["artifact_path"] is None
