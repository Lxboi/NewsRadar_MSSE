"""Sweeps `entry_filter.probability_min` against an already-trained,
already-promoted model over an already-built `features_v1` range, to
find a threshold this *specific* model's calibration can actually reach
while still keeping a net-of-costs edge -- rather than guessing a new
`strategy.yaml` value. Read-only: never writes config, never promotes,
never touches `strategy.yaml`. Prints a table; a human decides.

Roadmap: ad hoc addition, Phase 15 addendum. Motivated directly by a
real PAPER-mode run producing zero trades in ~20h: `entry_filter.
probability_up_min` (0.62, `strategy.yaml`) and `telegram.yaml`'s
`signal_filter.min_probability` (0.68) were both set generically in
Phase 11, before any real model existed, and are far above what the
`LogisticDirectionModel` promoted this session ever predicts (its own
calibration curve's top decile tops out around 0.26-0.30 for
BTC/ETH/SOL) -- so essentially every tick is rejected by `should_enter()`
before anything else is even checked. Confirmed live via `/signals`.

Reuses only already-tested primitives -- no new backtest/entry-filter
logic here, purely orchestration:

- `src.backtest.event_backtester`: `decisions_from_model()`/
  `EventBacktester` (Phase 10/11) -- the exact same argmax + entry-filter
  logic `should_enter()`/`PaperTradingEngine` use live, just swept
  across several `probability_min` values offline instead of the one
  fixed value in `strategy.yaml`.
- `src.backtest.metrics`: `compute_metrics()` (Phase 10) -- the same
  PnL/win-rate math `PerformanceTracker` already uses live.

A threshold's win rate here is judged the same way `evaluate.py`/
`train_model.py` already judge accuracy: a one-sided exact binomial test
against a 50/50 coin flip, not just "is win_rate > 50%" (a small n can
clear that by chance). **Multiple-comparisons caution applies just as
much here as it did to picking `(horizon_s, threshold_bps)`** -- picking
the best-looking `probability_min` out of several tried inflates
apparent significance versus a single pre-chosen value; treat this as
"which range looks worth re-validating on fresh data," not "here is the
final number."

Usage (from the project root, venv active), after
`scripts/build_feature_table.py` has already produced a `features_v1`
table covering `--start`/`--end`:

    python -m scripts.sweep_entry_threshold --asset BTC \\
        --start 2026-08-21 --end 2026-08-24 \\
        --horizon-s 60 --threshold-bps 10

Sweeps `DEFAULT_PROBABILITIES` by default (0.30 up to `strategy.yaml`'s
own 0.62, in 0.05 steps) -- pass `--probabilities` to override, e.g.
`--probabilities 0.30,0.35,0.40`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

import yaml
from scipy.stats import binomtest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

sys.path.insert(0, str(PROJECT_ROOT))

from src.backtest.event_backtester import EventBacktester, decisions_from_model  # noqa: E402
from src.backtest.metrics import compute_metrics  # noqa: E402
from src.models.model_persistence import load_model  # noqa: E402
from src.models.model_registry import ModelRegistry  # noqa: E402
from src.storage.parquet_reader import date_range, read_partitions  # noqa: E402

logger = logging.getLogger("sweep_entry_threshold")

DEFAULT_PROBABILITIES = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.62]


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_rows(features_root: Path, asset: str, dates) -> "list[dict]":
    # Same loading convention as scripts/train_model.py: build_feature_table.py
    # writes `timestamp` as an ISO string (no native tz-aware datetime
    # round-trip through pa.Table.from_pylist(dict) the way this codebase
    # writes Parquet) -- parsed back here since find_forward_indices()/
    # EventBacktester.run() both need a real datetime.
    rows = read_partitions(features_root, "features_v1", dates, asset=asset, sort_key="timestamp")
    for row in rows:
        row["timestamp"] = dt.datetime.fromisoformat(row["timestamp"])
    return rows


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--asset", required=True)
    p.add_argument("--start", required=True, help="Start date, YYYY-MM-DD (UTC, inclusive) of the features_v1 table to sweep over.")
    p.add_argument("--end", required=True, help="End date, YYYY-MM-DD (UTC, inclusive).")
    p.add_argument("--horizon-s", type=int, required=True, help="Must match the promoted/registered model's own horizon_s.")
    p.add_argument("--threshold-bps", type=float, required=True, help="Must match the promoted/registered model's own threshold_bps.")
    p.add_argument(
        "--model-version", default=None,
        help="A specific registered model_version to load instead of the currently promoted one "
        "(ModelRegistry.list_models()). Default: ModelRegistry.current_promoted() for this asset/horizon/threshold.",
    )
    p.add_argument(
        "--probabilities", default=None,
        help=f"Comma-separated probability_min values to sweep (default: {','.join(str(x) for x in DEFAULT_PROBABILITIES)}).",
    )
    return p.parse_args(argv)


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
    args = parse_args(argv)

    settings = load_yaml("settings.yaml")
    strategy_cfg = load_yaml("strategy.yaml")
    features_root = PROJECT_ROOT / settings["storage"]["features_dir"]
    models_root = PROJECT_ROOT / "data" / "models"
    registry = ModelRegistry(models_root / "registry.jsonl")

    # Same threshold_bps int-normalization fragility train_model.py's own
    # smoke-testing caught (argparse's type=float turns "10" into 10.0,
    # which compute_targets()'s plain-int-keyed target columns never
    # match) -- not exercised by this script directly, but the model was
    # trained/registered under the normalized int key, so loading it back
    # must use the same key.
    threshold_bps = args.threshold_bps
    if threshold_bps == int(threshold_bps):
        threshold_bps = int(threshold_bps)

    if args.model_version:
        record = next(
            (m for m in registry.list_models(asset=args.asset, horizon_s=args.horizon_s, threshold_bps=threshold_bps)
             if m["model_version"] == args.model_version),
            None,
        )
        if record is None:
            raise SystemExit(
                f"model_version={args.model_version!r} not found in registry for "
                f"asset={args.asset} horizon_s={args.horizon_s} threshold_bps={threshold_bps}"
            )
    else:
        record = registry.current_promoted(asset=args.asset, horizon_s=args.horizon_s, threshold_bps=threshold_bps)
        if record is None:
            raise SystemExit(
                f"no promoted model for asset={args.asset} horizon_s={args.horizon_s} threshold_bps={threshold_bps} "
                "-- pass --model-version to sweep a specific registered (not necessarily promoted) candidate instead"
            )

    artifact_path = record.get("artifact_path")
    if not artifact_path:
        raise SystemExit(f"model_version={record['model_version']!r} has no artifact_path recorded -- can't load it")
    model = load_model(models_root / artifact_path)
    logger.info("[%s] loaded model_version=%s", args.asset, record["model_version"])

    dates = date_range(
        dt.datetime.strptime(args.start, "%Y-%m-%d").date(),
        dt.datetime.strptime(args.end, "%Y-%m-%d").date(),
    )
    rows = _load_rows(features_root, args.asset, dates)
    if not rows:
        raise SystemExit(
            f"no features_v1 rows found for asset={args.asset} in {args.start}..{args.end} under {features_root} "
            "-- run scripts/build_feature_table.py for this range first"
        )
    logger.info("[%s] %d feature rows loaded", args.asset, len(rows))

    # Scored once -- direction/probability/expected_value_bps depend only
    # on the model and threshold_bps, not on probability_min, so every
    # swept threshold reuses the same decisions instead of re-scoring.
    decisions = decisions_from_model(model, rows, threshold_bps=threshold_bps)

    entry_filter_cfg = strategy_cfg["entry_filter"]
    costs_cfg = strategy_cfg["costs"]
    probabilities = (
        [float(x) for x in args.probabilities.split(",")] if args.probabilities else DEFAULT_PROBABILITIES
    )

    logger.info(
        "[%s] sweeping %d probability_min value(s) over %d rows (horizon_s=%s threshold_bps=%s)",
        args.asset, len(probabilities), len(rows), args.horizon_s, threshold_bps,
    )

    header = f"{'prob_min':>9} {'n_trades':>9} {'win_rate':>9} {'net_pnl':>12} {'expectancy':>11} {'p_value':>10}"
    print(header)
    print("-" * len(header))
    for prob_min in probabilities:
        backtester = EventBacktester(
            taker_fee_bps=costs_cfg["taker_fee_bps"],
            assumed_slippage_bps=costs_cfg["assumed_slippage_bps"],
            probability_min=prob_min,
            min_expected_value_bps=entry_filter_cfg["min_expected_value"],
            max_spread_bps=entry_filter_cfg["max_spread_bps"],
            allowed_regimes=tuple(entry_filter_cfg["allowed_regimes"]),
        )
        trades = backtester.run(rows, decisions, horizon_s=args.horizon_s)
        metrics = compute_metrics(trades)

        n = metrics["n_trades"]
        win_rate = metrics["win_rate"]
        p_value = None
        if n and win_rate is not None:
            n_wins = round(win_rate * n)
            p_value = binomtest(n_wins, n, p=0.5, alternative="greater").pvalue

        win_rate_str = "n/a" if win_rate is None else f"{win_rate:.1%}"
        expectancy = metrics["expectancy"]
        expectancy_str = "n/a" if expectancy is None else f"{expectancy:.3f}"
        p_value_str = "n/a" if p_value is None else f"{p_value:.2e}"
        print(f"{prob_min:>9.2f} {n:>9} {win_rate_str:>9} {metrics['net_pnl']:>12.2f} {expectancy_str:>11} {p_value_str:>10}")


if __name__ == "__main__":
    main()
