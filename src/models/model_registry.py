"""Tracks model_version, training_period, features, parameters,
performance for every candidate model (section 59). No model is
auto-deployed; promotion is a manual, logged decision.

Roadmap: PHASE 9. Implemented as an append-only JSONL log rather than a
database table with in-place mutation: section 59's requirement is really
an audit trail ("what did we know about this model, and when did we
decide to promote it"), and in-place mutation would destroy exactly the
history that makes an audit trail useful. Two event kinds share one file,
in write order:

- `"register"` — a candidate model was trained and evaluated. Written
  once per model_version; never updated afterward, since retroactively
  editing a model's recorded performance after the fact is the kind of
  thing this registry exists to prevent.

**`artifact_path` (Phase 15, optional, backward-compatible)**: where the
actual fitted model object was written via
`models/model_persistence.py: save_model()` — this registry itself still
never stores the model, only a path to it (an audit log stays lightweight
JSONL; the model artifact is scikit-learn-pipeline-sized). Left `None`
for older/registered-without-training-a-real-artifact records — every
reader (`list_models()`, `current_promoted()`) tolerates a missing key
via plain `dict.get()`, so this is additive, not a breaking schema
change to already-written registry files.
- `"promote"` — a human (or a documented process standing in for one)
  decided to make `model_version` the one currently live for a given
  `(asset, horizon_s, threshold_bps)`. `promote()` requires a `reason`
  and refuses to promote a `model_version` that was never `register()`'d
  — you can't promote a model this registry has no record of evaluating.
  Nothing in this module calls `promote()` on its own; nothing about
  crossing an accuracy/calibration threshold triggers it automatically.
  That is section 59's "no model is auto-deployed" in code, not just in
  the docstring.

`current_promoted()` answers "what's live right now" by taking the most
recent `promote` event for a `(asset, horizon_s, threshold_bps)` key and
looking up its matching `register` record — so "live" always resolves to
a full record (training period, features, parameters, performance), not
just a bare version string.
"""
from __future__ import annotations

import json
from pathlib import Path


class ModelRegistry:
    """See module docstring. Backed by a single append-only JSONL file at
    `path`; created (along with parent directories) if it doesn't exist
    yet. Safe to point multiple `ModelRegistry` instances at the same
    path — every read re-reads the file from disk rather than caching,
    so a registry opened earlier in a long-running process still sees
    records another process appended."""

    def __init__(self, path: "str | Path") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def register(
        self,
        *,
        model_version: str,
        asset: str,
        horizon_s: int,
        threshold_bps: float,
        model_kind: str,
        training_period: dict,
        features: "list[str]",
        parameters: dict,
        performance: dict,
        calibration: "dict | None" = None,
        notes: "str | None" = None,
        artifact_path: "str | None" = None,
    ) -> dict:
        """Records one candidate model. `training_period` is caller-defined
        (e.g. `{"start": "...", "end": "..."}`) — this module doesn't
        interpret it, just stores it verbatim for later audit. `features`
        should be the exact `feature_columns()` list the model was
        trained on, `parameters` the model's hyperparameters (e.g.
        `LogisticDirectionModel.C`), `performance` whatever evaluation
        metrics apply (`evaluate_direction_accuracy()`'s dict,
        `calibration.calibrate()`'s dict, or both — this module doesn't
        prescribe a shape). `artifact_path` (Phase 15, optional) records
        where `models/model_persistence.py: save_model()` wrote the
        actual fitted object, if it was saved — see the module docstring.
        Raises `ValueError` if `model_version` was already registered,
        since a version string is meant to be a unique identifier, not
        something silently overwritten."""
        if model_version in self._registered_versions():
            raise ValueError(f"model_version {model_version!r} is already registered")
        record = {
            "event": "register",
            "model_version": model_version,
            "asset": asset,
            "horizon_s": horizon_s,
            "threshold_bps": threshold_bps,
            "model_kind": model_kind,
            "training_period": training_period,
            "features": list(features),
            "parameters": parameters,
            "performance": performance,
            "calibration": calibration,
            "notes": notes,
            "artifact_path": artifact_path,
        }
        self._append(record)
        return record

    def promote(
        self,
        model_version: str,
        *,
        asset: str,
        horizon_s: int,
        threshold_bps: float,
        reason: str,
        promoted_by: "str | None" = None,
    ) -> dict:
        """Makes `model_version` the current live model for
        `(asset, horizon_s, threshold_bps)`. `reason` is required and
        non-empty — a promotion with no recorded justification defeats
        the point of an audit trail. Raises `ValueError` if
        `model_version` was never `register()`'d for this exact
        `(asset, horizon_s, threshold_bps)` key (a model registered for
        BTC/30s/10bps can't be promoted as the live ETH/60s/25bps model —
        those are different questions per `logistic_model.py`'s module
        docstring)."""
        if not reason or not reason.strip():
            raise ValueError("promote() requires a non-empty reason")
        registered = self._registered_versions(asset=asset, horizon_s=horizon_s, threshold_bps=threshold_bps)
        if model_version not in registered:
            raise ValueError(
                f"model_version {model_version!r} was never register()ed for "
                f"asset={asset!r}, horizon_s={horizon_s}, threshold_bps={threshold_bps}"
            )
        record = {
            "event": "promote",
            "model_version": model_version,
            "asset": asset,
            "horizon_s": horizon_s,
            "threshold_bps": threshold_bps,
            "reason": reason,
            "promoted_by": promoted_by,
        }
        self._append(record)
        return record

    def list_models(
        self,
        *,
        asset: "str | None" = None,
        horizon_s: "int | None" = None,
        threshold_bps: "float | None" = None,
    ) -> "list[dict]":
        """All `register()`ed records, oldest first, optionally filtered
        to one `(asset, horizon_s, threshold_bps)` key (any subset of the
        three may be given — `asset="BTC"` alone lists every model ever
        registered for BTC, across all horizons/thresholds)."""
        return [
            r for r in self._read_all()
            if r["event"] == "register" and self._matches(r, asset, horizon_s, threshold_bps)
        ]

    def list_promotions(
        self,
        *,
        asset: "str | None" = None,
        horizon_s: "int | None" = None,
        threshold_bps: "float | None" = None,
    ) -> "list[dict]":
        """Full promotion history, oldest first — every `promote()` call
        ever made (optionally filtered), not just the current one. This is
        the audit trail itself; `current_promoted()` is a convenience view
        over its most recent entry."""
        return [
            r for r in self._read_all()
            if r["event"] == "promote" and self._matches(r, asset, horizon_s, threshold_bps)
        ]

    def current_promoted(
        self,
        *,
        asset: str,
        horizon_s: int,
        threshold_bps: float,
    ) -> "dict | None":
        """The full `register()` record for whichever `model_version` is
        currently promoted for this `(asset, horizon_s, threshold_bps)`
        key, or `None` if nothing has ever been promoted for it. If the
        matching `register` record is somehow missing (should be
        impossible — `promote()` refuses to promote an unregistered
        version — but the file is hand-editable), raises `ValueError`
        rather than silently returning `None`, since that would look
        identical to "nothing promoted yet"."""
        promotions = self.list_promotions(asset=asset, horizon_s=horizon_s, threshold_bps=threshold_bps)
        if not promotions:
            return None
        latest = promotions[-1]
        for record in self.list_models(asset=asset, horizon_s=horizon_s, threshold_bps=threshold_bps):
            if record["model_version"] == latest["model_version"]:
                return record
        raise ValueError(
            f"promoted model_version {latest['model_version']!r} has no matching register() record — "
            "registry file may have been edited by hand"
        )

    def _registered_versions(
        self,
        *,
        asset: "str | None" = None,
        horizon_s: "int | None" = None,
        threshold_bps: "float | None" = None,
    ) -> "set[str]":
        return {r["model_version"] for r in self.list_models(asset=asset, horizon_s=horizon_s, threshold_bps=threshold_bps)}

    @staticmethod
    def _matches(
        record: dict,
        asset: "str | None",
        horizon_s: "int | None",
        threshold_bps: "float | None",
    ) -> bool:
        if asset is not None and record["asset"] != asset:
            return False
        if horizon_s is not None and record["horizon_s"] != horizon_s:
            return False
        if threshold_bps is not None and record["threshold_bps"] != threshold_bps:
            return False
        return True

    def _append(self, record: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def _read_all(self) -> "list[dict]":
        records = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records
