"""Saves/loads a fitted model object to/from disk (section 30, extended
for Phase 15's live wiring).

Roadmap: PHASE 15. New (not a Phase 1 stub). `ModelRegistry` (Phase 9)
was deliberately built as metadata-only — an append-only JSONL audit
trail of *what was trained and when it was promoted*, never the fitted
object itself (see that module's docstring: it's an audit log, not a
model store). That was the right scope for Phase 9, which only needed
`fit()`/`predict_proba()` in-memory within one training run. Phase 15's
live paper-trading loop is the first caller that needs to load a model
*back*, in a different process, after training — a real gap Phase 9
correctly left unaddressed rather than guessing at a storage format
before there was a real consumer for it.

`save_model()`/`load_model()` are deliberately generic (`joblib.dump`/
`joblib.load`) rather than a bespoke serialization format: every model
this codebase produces (`LogisticDirectionModel`, `TreeDirectionModel`,
`calibration.py: CalibratedDirectionModel`) is a plain Python object
wrapping a fitted scikit-learn `Pipeline` with no unpicklable state
(open file handles, thread locks, ...) — `joblib` is scikit-learn's own
documented recommendation for this exact case, and is already a
transitive dependency via `scikit-learn` itself (`requirements.txt`
pins it explicitly below anyway, so a future scikit-learn release
un-bundling it can't silently break this module).

`load_model()` optionally takes `expected_class` — a live caller (Phase
15's `src/live/trading_loop.py`) always knows which model class it asked
`scripts/train_model.py` to train, so this catches "the artifact on disk
is a `TreeDirectionModel` but the caller expected a
`LogisticDirectionModel`" as a clear `TypeError` at load time, not a
confusing `AttributeError` several calls later when `predict_proba()`
happens to behave differently than assumed.
"""
from __future__ import annotations

from pathlib import Path

import joblib


def save_model(model, path: "str | Path") -> Path:
    """Writes `model` (any fitted object — typically a
    `LogisticDirectionModel`/`TreeDirectionModel`/
    `CalibratedDirectionModel`) to `path` via `joblib.dump`, creating
    parent directories if needed. Returns the resolved `Path` written,
    for a caller (e.g. `scripts/train_model.py`) to record as
    `ModelRegistry.register(..., artifact_path=...)`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, path)
    return path


def load_model(path: "str | Path", *, expected_class: "type | None" = None):
    """Loads a model previously written by `save_model()`. Raises
    `FileNotFoundError` (via the underlying open, not a bespoke message)
    if `path` doesn't exist — never returns `None` for a missing
    artifact, since a live caller silently trading with `model=None`
    would crash confusingly downstream instead of failing loudly at
    startup where the problem is obvious. If `expected_class` is given
    and the loaded object isn't an instance of it, raises `TypeError`
    naming both the expected and actual class, rather than letting a
    class mismatch surface later as a confusing `predict_proba()`
    failure or — worse — a silently-different prediction shape."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no model artifact at {path}")
    model = joblib.load(path)
    if expected_class is not None and not isinstance(model, expected_class):
        raise TypeError(
            f"model artifact at {path} is a {type(model).__name__}, "
            f"expected {expected_class.__name__}"
        )
    return model
