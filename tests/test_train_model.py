"""Phase 15 tests: scripts/train_model.py -- load features_v1 rows ->
chronological purged split -> fit -> evaluate -> save -> register ->
optionally promote. Previously only manually smoke-tested (see the
module's own commit history/docs); this is the permanent regression
suite for it.

Builds a real, on-disk features_v1 Parquet table via ParquetStore (the
same writer scripts/build_feature_table.py uses) under tmp_path, and
monkeypatches the script's module-level PROJECT_ROOT/CONFIG_DIR so
nothing here ever touches the real repo's config/ or data/ directories
-- both are plain module globals read at call time (not baked into
argparse defaults), so monkeypatching them redirects every path the
script computes (features_root, artifacts_dir, registry path, and
config/settings.yaml itself).
"""
from __future__ import annotations

import datetime as dt
import importlib

import pytest
import yaml

import scripts.train_model as train_model
from src.storage.parquet_store import ParquetStore
from src.models.model_registry import ModelRegistry
from src.models.model_persistence import load_model
from src.live.trading_loop import load_promoted_model

ASSET = "BTC"
HORIZON_S = 30
THRESHOLD_BPS = 10
DAY = dt.date(2026, 1, 1)


def _write_synthetic_news_features_table(root, n=800, asset=ASSET):
    """Same shape as `_write_synthetic_features_table()`, but the
    learnable signal is `news_score` (alternating sign) rather than
    `obi_10` -- `--model news` (Phase 15 addendum 7) trains on exactly
    the news_* columns and would raise if they were absent, which the
    default fixture doesn't provide."""
    store = ParquetStore(root=root, flush_every=n + 1)
    base = dt.datetime.combine(DAY, dt.time(0, 0, 0), tzinfo=dt.timezone.utc)
    for i in range(n):
        up = i % 2 == 0
        ts = base + dt.timedelta(seconds=i)
        score = (0.3 + 0.7 * (i % 5) / 5.0) if up else -(0.3 + 0.7 * (i % 5) / 5.0)
        row = {
            "asset": asset,
            "timestamp": ts.isoformat(),
            "news_score": score,
            "news_importance": 0.5,
            "news_novelty": 0.5,
            "news_age": 60.0,
            "news_credibility": 0.5,
            "market_regime": "NORMAL_VOL",
            f"target_{HORIZON_S}s_up_{THRESHOLD_BPS}bps": 1 if up else 0,
            f"target_{HORIZON_S}s_down_{THRESHOLD_BPS}bps": 0 if up else 1,
        }
        store.write_row("features_v1", asset, row, ts_ns=int(ts.timestamp() * 1_000_000_000))
    store.flush_all()


def _write_synthetic_features_table(root, n=800, asset=ASSET, seed_regime="NORMAL_VOL"):
    """n rows, 1 second apart starting at 00:00:00 UTC on DAY, with
    obi_10/targets correlated the same way tests/test_model_persistence.py's
    make_rows() correlates them -- alternating up/down every other row --
    so a fitted LogisticDirectionModel has real, non-degenerate signal to
    learn (a constant-target table would make some downstream checks,
    e.g. beats_coin_flip, meaningless by construction)."""
    store = ParquetStore(root=root, flush_every=n + 1)
    base = dt.datetime.combine(DAY, dt.time(0, 0, 0), tzinfo=dt.timezone.utc)
    for i in range(n):
        up = i % 2 == 0
        ts = base + dt.timedelta(seconds=i)
        row = {
            "asset": asset,
            "timestamp": ts.isoformat(),
            "obi_10": (0.3 + 0.7 * (i % 5) / 5.0) if up else -(0.3 + 0.7 * (i % 5) / 5.0),
            "market_regime": seed_regime,
            f"target_{HORIZON_S}s_up_{THRESHOLD_BPS}bps": 1 if up else 0,
            f"target_{HORIZON_S}s_down_{THRESHOLD_BPS}bps": 0 if up else 1,
        }
        store.write_row("features_v1", asset, row, ts_ns=int(ts.timestamp() * 1_000_000_000))
    store.flush_all()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Sets up an isolated fake project root: config/settings.yaml
    pointing storage.features_dir at a real synthetic features_v1 table,
    with train_model's module globals redirected there."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    features_dir = tmp_path / "data" / "features"
    (config_dir / "settings.yaml").write_text(
        yaml.dump({"storage": {"features_dir": "data/features"}}), encoding="utf-8",
    )
    _write_synthetic_features_table(features_dir)

    monkeypatch.setattr(train_model, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(train_model, "CONFIG_DIR", config_dir)
    return tmp_path


@pytest.fixture
def news_project(tmp_path, monkeypatch):
    """Same as `project`, but the synthetic table has news_* columns
    with a learnable signal instead of `obi_10` -- `--model news`
    (Phase 15 addendum 7) requires these and would raise on `project`'s
    default table."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    features_dir = tmp_path / "data" / "features"
    (config_dir / "settings.yaml").write_text(
        yaml.dump({"storage": {"features_dir": "data/features"}}), encoding="utf-8",
    )
    _write_synthetic_news_features_table(features_dir)

    monkeypatch.setattr(train_model, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(train_model, "CONFIG_DIR", config_dir)
    return tmp_path


def _argv(**overrides):
    args = {
        "--asset": ASSET, "--start": DAY.isoformat(), "--end": DAY.isoformat(),
        "--horizon-s": str(HORIZON_S), "--threshold-bps": str(THRESHOLD_BPS),
    }
    args.update(overrides)
    flat = []
    for k, v in args.items():
        flat.append(k)
        if v is not None:
            flat.append(str(v))
    return flat


def test_end_to_end_registers_but_does_not_promote_by_default(project):
    train_model.main(_argv())

    registry = ModelRegistry(project / "data" / "models" / "registry.jsonl")
    models = registry.list_models(asset=ASSET, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS)
    assert len(models) == 1
    record = models[0]
    assert record["model_kind"] == "logistic"
    assert record["artifact_path"] is not None
    # artifact_path is relative to data/models (== model_registry_root),
    # not to the project root -- see the comment on this line in
    # train_model.py for why that distinction matters.
    assert (project / "data" / "models" / record["artifact_path"]).exists()
    assert registry.current_promoted(asset=ASSET, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS) is None


def test_promoted_model_actually_loads_via_load_promoted_model(project):
    # Regression test for a real bug: train_model.py registered
    # artifact_path relative to PROJECT_ROOT, but
    # src/live/trading_loop.py's load_promoted_model() (the function
    # main.py actually wires into PaperTradingEngine) joins it as
    # `model_registry_root / artifact_path` where model_registry_root is
    # already data/models -- so the two conventions disagreed and the
    # real loader raised FileNotFoundError on a doubled
    # "data/models/data/models/..." path. Caught in production (Phase 15
    # first live PAPER run on the Pi, 2026-08-25) precisely because
    # train_model.py and load_promoted_model were only ever tested in
    # isolation before. This test exercises the real path both scripts
    # actually take, end-to-end, with no shortcuts.
    train_model.main(_argv(**{"--promote": None, "--reason": "regression test"}))

    registry = ModelRegistry(project / "data" / "models" / "registry.jsonl")
    result = load_promoted_model(registry, project / "data" / "models", ASSET, HORIZON_S, THRESHOLD_BPS)
    assert result is not None
    assert result["model"].predict_proba is not None


def test_promote_requires_reason(project):
    with pytest.raises(SystemExit, match="--promote requires --reason"):
        train_model.main(_argv(**{"--promote": None}))


def test_promote_with_reason_makes_the_model_current(project):
    train_model.main(_argv(**{"--promote": None, "--reason": "test promotion"}))

    registry = ModelRegistry(project / "data" / "models" / "registry.jsonl")
    promoted = registry.current_promoted(asset=ASSET, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS)
    assert promoted is not None
    promotions = registry.list_promotions(asset=ASSET, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS)
    assert promotions[-1]["reason"] == "test promotion"


def test_threshold_bps_whole_float_is_normalized_to_int_so_targets_resolve(project):
    # Regression test for the exact bug this script's own smoke-testing
    # caught: --threshold-bps is declared type=float in argparse, so
    # "10" becomes 10.0 -- compute_targets() always writes plain-int
    # threshold columns ("target_30s_up_10bps"), so an unnormalized
    # 10.0 would silently resolve zero targets and blow up several calls
    # later as "nothing to train on". This must not raise.
    train_model.main(_argv(**{"--threshold-bps": "10.0"}))
    registry = ModelRegistry(project / "data" / "models" / "registry.jsonl")
    models = registry.list_models(asset=ASSET, horizon_s=HORIZON_S)
    assert len(models) == 1
    # Stored as the normalized int, matching compute_targets()'s own key format.
    assert models[0]["threshold_bps"] == 10


def test_calibrate_flag_saves_a_calibrated_model_that_still_predicts(project):
    train_model.main(_argv(**{"--calibrate": None}))
    registry = ModelRegistry(project / "data" / "models" / "registry.jsonl")
    record = registry.list_models(asset=ASSET, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS)[0]
    assert "calibrated" in record["model_kind"]

    loaded = load_model(project / "data" / "models" / record["artifact_path"])
    sample_row = {"asset": ASSET, "obi_10": 0.8, "market_regime": "NORMAL_VOL"}
    proba = loaded.predict_proba([sample_row])
    assert len(proba) == 1


def test_tree_model_choice_is_respected(project):
    train_model.main(_argv(**{"--model": "tree"}))
    registry = ModelRegistry(project / "data" / "models" / "registry.jsonl")
    record = registry.list_models(asset=ASSET, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS)[0]
    assert record["model_kind"] == "tree"


def test_news_model_choice_is_respected_end_to_end(news_project):
    train_model.main(_argv(**{"--model": "news"}))
    registry = ModelRegistry(news_project / "data" / "models" / "registry.jsonl")
    record = registry.list_models(asset=ASSET, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS)[0]
    assert record["model_kind"] == "news"
    # the synthetic news_score signal is trivially separable, same shape
    # as _write_synthetic_features_table()'s obi_10 -- CV should find it.
    assert record["performance"]["directional_accuracy"]["n_scored"] > 0


def test_news_model_fails_clearly_on_a_table_without_news_columns(project):
    # `project`'s default synthetic table has no news_* columns at all --
    # every CV fold fails to fit, so main() hits the addendum 6
    # zero-folds-used guard rather than silently registering a
    # degenerate model.
    with pytest.raises(SystemExit, match="CV folds were skipped"):
        train_model.main(_argv(**{"--model": "news"}))


def test_no_rows_found_raises_a_clear_system_exit(project):
    with pytest.raises(SystemExit, match="no features_v1 rows found"):
        train_model.main(_argv(**{"--start": "2099-01-01", "--end": "2099-01-01"}))


def test_calibration_fraction_too_large_leaves_an_empty_side(project):
    with pytest.raises(SystemExit, match="calibration split left an empty side"):
        train_model.main(_argv(**{"--calibrate": None, "--calibration-fraction": "0.999"}))


def test_final_model_is_fit_on_all_rows_when_not_calibrating(project):
    # Phase 15 addendum 6: without --calibrate there's no reason to
    # withhold any row from the model that actually gets saved/
    # registered -- CV (a separate step) already produced the honest
    # evaluation number, so the final fit uses everything.
    train_model.main(_argv())
    registry = ModelRegistry(project / "data" / "models" / "registry.jsonl")
    record = registry.list_models(asset=ASSET, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS)[0]
    assert record["training_period"]["n_train_rows"] == record["training_period"]["n_rows_total"]
    assert record["training_period"]["n_calibration_rows"] == 0


def test_cv_folds_flag_is_recorded_and_defaults_to_five(project):
    train_model.main(_argv())
    registry = ModelRegistry(project / "data" / "models" / "registry.jsonl")
    record = registry.list_models(asset=ASSET, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS)[0]
    assert record["training_period"]["cv_folds"] == train_model.DEFAULT_CV_FOLDS
    assert record["training_period"]["n_cv_folds_used"] >= 1


def test_cv_pools_across_folds_so_a_quiet_last_block_still_gets_scored(project):
    # Regression test for the exact real-world failure this addendum
    # fixes: a synthetic table where every directional target row is
    # concentrated in the FIRST half only -- a single last-block
    # holdout (the pre-addendum-6 behavior) would score n=0 on this
    # table every time, no matter how much quiet data follows. Purged
    # k-fold CV must still find and score those rows because every row
    # gets a turn in some fold's validation split, not just the last one.
    quiet_root = project / "data" / "features"
    from src.storage.parquet_store import ParquetStore

    store = ParquetStore(root=quiet_root, flush_every=10_000)
    base = dt.datetime.combine(DAY, dt.time(0, 0, 0), tzinfo=dt.timezone.utc)
    n = 400
    for i in range(n):
        directional = i < n // 2  # all signal in the first half, chronologically
        up = i % 2 == 0
        ts = base + dt.timedelta(seconds=i)
        row = {
            "asset": ASSET,
            "timestamp": ts.isoformat(),
            "obi_10": (0.3 + 0.7 * (i % 5) / 5.0) if (directional and up) else (-(0.3 + 0.7 * (i % 5) / 5.0) if directional else 0.0),
            "market_regime": "NORMAL_VOL",
            f"target_{HORIZON_S}s_up_{THRESHOLD_BPS}bps": 1 if (directional and up) else 0,
            f"target_{HORIZON_S}s_down_{THRESHOLD_BPS}bps": 1 if (directional and not up) else 0,
        }
        store.write_row("features_v1", ASSET, row, ts_ns=int(ts.timestamp() * 1_000_000_000))
    store.flush_all()

    train_model.main(_argv(**{"--cv-folds": "4"}))
    registry = ModelRegistry(project / "data" / "models" / "registry.jsonl")
    record = registry.list_models(asset=ASSET, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS)[0]
    # The whole point: pooled CV scored *something*, unlike a last-block
    # holdout on this table which would see zero directional examples.
    assert record["performance"]["directional_accuracy"]["n_scored"] > 0


def test_event_level_accuracy_recorded_in_registry_performance(project):
    # Phase 15 addendum 8: every registered model now carries an
    # event-level diagnostic alongside the row-level one, so a future
    # reader of the registry (human or code) can't miss it.
    train_model.main(_argv())
    registry = ModelRegistry(project / "data" / "models" / "registry.jsonl")
    record = registry.list_models(asset=ASSET, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS)[0]
    event_level = record["performance"]["event_level_accuracy"]
    assert set(event_level.keys()) == {
        "n_clusters", "n_majority_correct", "accuracy", "p_value", "beats_coin_flip", "top_cluster_share",
    }
    assert event_level["n_clusters"] >= 1


def test_event_level_diagnostics_empty_details_returns_none_shape():
    result = train_model._event_level_diagnostics([], horizon_s=60)
    assert result == {
        "n_clusters": 0, "n_majority_correct": 0.0, "accuracy": None,
        "p_value": None, "beats_coin_flip": False, "top_cluster_share": None,
    }


def test_event_level_diagnostics_collapses_one_repeated_burst_into_one_cluster():
    # The exact false-positive shape addendum 8 exists to catch (found
    # for real on SOL/300s/100bps this session): 116 consecutive scored
    # rows, one second apart, all "correct" -- overwhelming row-level
    # evidence (n=116, 100% correct) that is really one real event
    # repeated many times over, not 116 independent trials.
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    details = [(base + dt.timedelta(seconds=i), True) for i in range(116)]
    result = train_model._event_level_diagnostics(details, horizon_s=60)
    assert result["n_clusters"] == 1
    assert result["top_cluster_share"] == 1.0
    # A single trial can never itself be "significant" against p=0.5,
    # no matter how many rows it was built from.
    assert result["beats_coin_flip"] is False


def test_event_level_diagnostics_counts_widely_spaced_events_as_separate_trials():
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    # 20 separate events, each 1 hour apart (>> horizon_s), 15 correct.
    details = [(base + dt.timedelta(hours=i), i < 15) for i in range(20)]
    result = train_model._event_level_diagnostics(details, horizon_s=60)
    assert result["n_clusters"] == 20
    assert result["accuracy"] == pytest.approx(0.75)
    assert result["top_cluster_share"] == pytest.approx(1 / 20)


def test_event_level_diagnostics_exact_tie_within_a_cluster_counts_as_half():
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    cluster_a = [(base + dt.timedelta(seconds=i), i % 2 == 0) for i in range(4)]  # 2 correct, 2 wrong -> tie
    cluster_b_start = base + dt.timedelta(hours=1)
    cluster_b = [(cluster_b_start + dt.timedelta(seconds=i), True) for i in range(3)]  # all correct
    result = train_model._event_level_diagnostics(cluster_a + cluster_b, horizon_s=60)
    assert result["n_clusters"] == 2
    assert result["n_majority_correct"] == pytest.approx(1.5)


def _price_synced_rows(n=800, *, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS, move_bps=50.0, asset=ASSET):
    """`n` rows, 1s apart, `obi_10` alternating sign (same trivially-
    learnable-by-a-linear-model shape every other fixture in this file
    uses) -- but here `price` is constructed so that row `i`'s *actual*
    forward return at `horizon_s` is exactly `sign(obi_10[i]) *
    move_bps`, for every row with enough future history (`i < n -
    horizon_s`). `price[i + horizon_s]` is defined recursively from
    `price[i]` (`price[0..horizon_s-1]` is the flat base case) --
    `find_forward_prices()`'s exact-match lookup (1s sampling, integer
    horizon_s) makes this land precisely on the intended row, not an
    approximation. This is what makes `_net_expected_value()`'s
    hand-computable-in-expectation tests below possible: a perfectly
    separable classifier (`obi_10`, already proven learnable in
    test_logistic_model.py/test_news_model.py) plus a *known* realized
    move size means `predicted * realized_bps - cost` is
    `move_bps - cost` for essentially every scored row, not just a
    correct/incorrect coin flip."""
    base = dt.datetime.combine(DAY, dt.time(0, 0, 0), tzinfo=dt.timezone.utc)
    signs = [1.0 if i % 2 == 0 else -1.0 for i in range(n)]
    prices = [0.0] * n
    for i in range(min(horizon_s, n)):
        prices[i] = 100.0
    for i in range(n - horizon_s):
        prices[i + horizon_s] = prices[i] * (1 + signs[i] * move_bps / 10_000)

    rows = []
    for i in range(n):
        up = signs[i] > 0
        ts = base + dt.timedelta(seconds=i)
        rows.append({
            "asset": asset,
            "timestamp": ts,
            "price": prices[i],
            "obi_10": signs[i] * (0.3 + 0.7 * (i % 5) / 5.0),
            "market_regime": "NORMAL_VOL",
            f"target_{horizon_s}s_up_{threshold_bps}bps": 1 if up else 0,
            f"target_{horizon_s}s_down_{threshold_bps}bps": 0 if up else 1,
        })
    return rows


def test_forward_return_bps_matches_a_hand_computed_example():
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    rows = [
        {"timestamp": base, "price": 100.0},
        {"timestamp": base + dt.timedelta(seconds=1), "price": 101.0},
        {"timestamp": base + dt.timedelta(seconds=2), "price": 100.5},
        # no row far enough ahead of this one for horizon_s=2 -- must be None.
        {"timestamp": base + dt.timedelta(seconds=3), "price": 99.0},
    ]
    result = train_model._forward_return_bps(rows, horizon_s=2)
    assert result[0] == pytest.approx((100.5 / 100.0 - 1) * 10_000)  # row 0 -> row 2
    assert result[1] == pytest.approx((99.0 / 101.0 - 1) * 10_000)   # row 1 -> row 3
    assert result[2] is None
    assert result[3] is None


def test_net_expected_value_is_positive_when_the_edge_clears_a_low_cost():
    rows = _price_synced_rows(move_bps=50.0)
    from src.models.logistic_model import LogisticDirectionModel
    result = train_model._net_expected_value(
        rows, LogisticDirectionModel, HORIZON_S, THRESHOLD_BPS, k=4, round_trip_cost_bps=5.0,
    )
    assert result["round_trip_cost_bps"] == 5.0
    for level in ("row_level", "event_level"):
        r = result[level]
        assert r["n"] > 0
        # move_bps=50, cost=5 -> ~+45bps/trade in expectation; a wide
        # tolerance since a handful of rows near a fold boundary or a
        # near-decision-boundary fit can land on the "wrong" side.
        assert r["mean_net_pnl_bps"] > 20.0
        assert r["beats_breakeven"] is True


def test_net_expected_value_is_negative_when_cost_exceeds_the_edge():
    rows = _price_synced_rows(move_bps=50.0)
    from src.models.logistic_model import LogisticDirectionModel
    result = train_model._net_expected_value(
        rows, LogisticDirectionModel, HORIZON_S, THRESHOLD_BPS, k=4, round_trip_cost_bps=500.0,
    )
    for level in ("row_level", "event_level"):
        r = result[level]
        assert r["n"] > 0
        assert r["mean_net_pnl_bps"] < 0
        assert r["beats_breakeven"] is False


def test_net_expected_value_event_level_has_fewer_or_equal_trials_than_row_level():
    # Consecutive scored rows within horizon_s of each other collapse
    # into one event-level trial, same clustering _event_level_
    # diagnostics() uses -- this fixture's rows are 1s apart with
    # horizon_s=30, so almost the entire table collapses into a
    # handful of clusters, not one-trial-per-row.
    rows = _price_synced_rows(move_bps=50.0)
    from src.models.logistic_model import LogisticDirectionModel
    result = train_model._net_expected_value(
        rows, LogisticDirectionModel, HORIZON_S, THRESHOLD_BPS, k=4, round_trip_cost_bps=5.0,
    )
    assert 0 < result["event_level"]["n"] < result["row_level"]["n"]


def test_net_expected_value_empty_when_nothing_scored():
    # project's default fixture rows have no "price" key at all --
    # _forward_return_bps() must return None for every row rather than
    # KeyError, and _net_expected_value() must degrade to n=0 cleanly.
    rows = [
        {
            "asset": ASSET, "timestamp": dt.datetime.combine(DAY, dt.time(0, 0, 0), tzinfo=dt.timezone.utc),
            "obi_10": 0.5, "market_regime": "NORMAL_VOL",
            f"target_{HORIZON_S}s_up_{THRESHOLD_BPS}bps": 1, f"target_{HORIZON_S}s_down_{THRESHOLD_BPS}bps": 0,
        }
        for _ in range(50)
    ]
    from src.models.logistic_model import LogisticDirectionModel
    result = train_model._net_expected_value(
        rows, LogisticDirectionModel, HORIZON_S, THRESHOLD_BPS, k=2, round_trip_cost_bps=5.0,
    )
    assert result["row_level"] == {"n": 0, "mean_net_pnl_bps": None, "p_value": None, "beats_breakeven": False}
    assert result["event_level"] == {"n": 0, "mean_net_pnl_bps": None, "p_value": None, "beats_breakeven": False}


def test_round_trip_cost_bps_flag_wires_into_registry_performance(project):
    # project's synthetic table has no "price" column, so n stays 0 here
    # -- this test is purely about wiring (the flag reaches the registry
    # with the right shape), not about the computed numbers.
    train_model.main(_argv(**{"--round-trip-cost-bps": "9.0"}))
    registry = ModelRegistry(project / "data" / "models" / "registry.jsonl")
    record = registry.list_models(asset=ASSET, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS)[0]
    ev = record["performance"]["net_expected_value"]
    assert ev["round_trip_cost_bps"] == 9.0
    assert set(ev["row_level"].keys()) == {"n", "mean_net_pnl_bps", "p_value", "beats_breakeven"}
    assert set(ev["event_level"].keys()) == {"n", "mean_net_pnl_bps", "p_value", "beats_breakeven"}


def test_round_trip_cost_bps_flag_omitted_leaves_net_expected_value_none(project):
    train_model.main(_argv())
    registry = ModelRegistry(project / "data" / "models" / "registry.jsonl")
    record = registry.list_models(asset=ASSET, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS)[0]
    assert record["performance"]["net_expected_value"] is None


def test_purged_kfold_split_purges_both_edges_of_each_held_out_fold(project):
    rows = [
        {"timestamp": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=i)}
        for i in range(100)
    ]
    folds = train_model._purged_kfold_split(rows, k=4, horizon_s=5)
    assert len(folds) == 4
    for fold in folds:
        val_ts = {r["timestamp"] for r in fold["validation"]}
        train_ts = {r["timestamp"] for r in fold["train"]}
        assert val_ts.isdisjoint(train_ts)
        if fold["validation"]:
            lo, hi = min(val_ts), max(val_ts)
            purge = dt.timedelta(seconds=5)
            for t in train_ts:
                assert t < lo - purge or t > hi + purge


def test_registered_features_match_the_models_own_feature_columns(project):
    train_model.main(_argv())
    registry = ModelRegistry(project / "data" / "models" / "registry.jsonl")
    record = registry.list_models(asset=ASSET, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS)[0]
    assert "obi_10" in record["features"]
    assert "market_regime" in record["features"]
    assert not any(f.startswith("target_") for f in record["features"])


def test_two_registrations_a_second_apart_get_distinct_model_versions(project):
    # model_version embeds a second-resolution UTC timestamp
    # (dt.datetime.now(...).strftime("%Y%m%dT%H%M%SZ")) -- two calls
    # within the same wall-clock second would collide and raise on the
    # second register() (ModelRegistry rejects duplicate model_version).
    # A >=1s gap is what actually guarantees distinctness today; this
    # test documents that real constraint rather than assuming
    # uniqueness is unconditional.
    import time
    train_model.main(_argv())
    time.sleep(1.1)
    train_model.main(_argv())
    registry = ModelRegistry(project / "data" / "models" / "registry.jsonl")
    models = registry.list_models(asset=ASSET, horizon_s=HORIZON_S, threshold_bps=THRESHOLD_BPS)
    assert len(models) == 2
    assert models[0]["model_version"] != models[1]["model_version"]
