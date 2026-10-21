"""Phase 15 addendum 7: a directional model restricted to just the
news-derived feature columns -- `news_score`, `news_importance`,
`news_novelty`, `news_age`, `news_credibility` -- instead of
`LogisticDirectionModel`'s full ~100-column microstructure+news mix.

Motivation, grounded in real collected data rather than guessed: an ad
hoc `sign(news_score)` vs. actual-direction check (addendum 6's
follow-up, run directly against the real `features_v1` table) found a
real, statistically significant edge for BTC/SOL at horizon_s=60,
threshold_bps=10 -- accuracy 51.6-53.8%, p-values as low as 1e-11, on
samples of 8,600-20,300 decisive rows. But `LogisticDirectionModel`
trained on the same rows, with the news columns mixed among 100+ mostly
noisy microstructure ones, essentially never produces a confident call
(addendum 5: ~0.3% of rows). This model isolates the news signal to
test, under the same purged k-fold CV harness every other model in this
project is evaluated with (not the ad hoc sign-comparison, which has no
train/validation split and could be picking up look-ahead leakage or
other artifacts), whether a properly trained classifier on just these 5
columns reproduces that edge.

Reuses `LogisticDirectionModel`'s pipeline/contract entirely --
`fit()`/`predict_proba()` shape, calibration compatibility, purged CV
compatibility -- via the `_feature_columns()` override point that
refactor introduced. Registered as `--model news` in
`scripts/train_model.py`'s `MODEL_CLASSES`, so it plugs into the exact
same CLI/CV/registry/promotion flow as `logistic`/`tree` with no other
change.
"""
from __future__ import annotations

from src.models.logistic_model import LogisticDirectionModel

NEWS_FEATURE_COLUMNS = (
    "news_score",
    "news_importance",
    "news_novelty",
    "news_age",
    "news_credibility",
)


class NewsDirectionModel(LogisticDirectionModel):
    """Same logistic-regression pipeline as `LogisticDirectionModel`,
    trained on exactly `NEWS_FEATURE_COLUMNS` -- nothing else. Raises
    `ValueError` up front (via `_feature_columns()`, called from `fit()`
    before any sklearn work starts) if a row is missing one of these
    columns, rather than silently imputing a whole feature to NaN-only,
    which would make the model degenerate without any obvious error."""

    def _feature_columns(self, rows: "list[dict]") -> "list[str]":
        available: "set[str]" = set()
        for row in rows:
            available.update(row.keys())
        missing = [c for c in NEWS_FEATURE_COLUMNS if c not in available]
        if missing:
            raise ValueError(
                f"rows are missing news feature column(s) {missing} -- "
                "NewsDirectionModel requires features_v1's news_* columns "
                "(build_feature_table.py writes these; a table built before "
                "news joining was added, or with news collection disabled, "
                "won't have them)"
            )
        return list(NEWS_FEATURE_COLUMNS)
