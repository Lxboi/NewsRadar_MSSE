"""Order-book-level backtester for microstructure-only strategies
(no news dependency), used to isolate order-flow edge (section 44).

Roadmap: PHASE 10. `EventBacktester` already backtests any decision
function against any row table — including one where a model/baseline
simply doesn't use the news columns. But "doesn't use" by discipline
isn't the same guarantee as "cannot use": a feature-importance bug, a
`ColumnTransformer` change that starts including a column it shouldn't,
or a baseline kind added later that happens to read `news_score` would
silently reintroduce a news dependency into what was meant to be a pure
order-flow ablation. `strip_news_columns()` enforces the ablation
structurally instead: every `news_*`-prefixed key is *removed* from the
row dicts, not zeroed or defaulted, so a `row["news_score"]` access
raises `KeyError` immediately rather than a model quietly training on a
column that happens to always be `0.0`/`None`.

## How to actually run a news-free ablation

Stripping has to happen **before** a decision function ever sees the
rows, not just before `EventBacktester.run()` — by the time `run()` is
called, the `decisions` list already exists, and stripping columns out
of `rows` at that point can't retroactively un-train a model or un-score
a baseline that already had news access. The correct sequence:

```python
from src.backtest.orderbook_backtester import strip_news_columns
from src.backtest.event_backtester import decisions_from_model, EventBacktester

news_free_rows = strip_news_columns(rows)
model.fit(news_free_rows)                      # trained with no news columns to learn from
decisions = decisions_from_model(model, news_free_rows, threshold_bps=10)
trades = OrderBookBacktester().run(news_free_rows, decisions, horizon_s=30)
```

`OrderBookBacktester` itself is a thin `EventBacktester` subclass whose
`run()` strips news columns from `rows` before simulating fills — a
second, redundant safety net for the parts of `EventBacktester.run()`
that read `rows` directly (`spread`, `market_regime`, `price`,
`timestamp`, `asset` — never news), not a substitute for stripping
before scoring. Comparing `EventBacktester` results (news-eligible) with
`OrderBookBacktester` results (news-free) on the same underlying period
is what actually answers section 44's question: how much of the edge, if
any, survives when news access is removed by construction, not just by
convention.
"""
from __future__ import annotations

from src.backtest.event_backtester import EventBacktester

NEWS_COLUMN_PREFIX = "news_"


def strip_news_columns(rows: "list[dict]") -> "list[dict]":
    """Returns new row dicts with every `news_*`-prefixed key removed
    entirely (not set to `None`/`0.0`) — see module docstring. Does not
    mutate `rows`. Column names come from `FeatureEngine._news_columns()`
    (`news_score`, `news_importance`, `news_novelty`, `news_age`,
    `news_credibility`) via the shared prefix, so a new news-derived
    column added there in a later phase is stripped automatically as
    long as it keeps the `news_` naming convention — no name list to
    keep in sync by hand."""
    return [{k: v for k, v in row.items() if not k.startswith(NEWS_COLUMN_PREFIX)} for row in rows]


class OrderBookBacktester(EventBacktester):
    """`EventBacktester` (same constructor, same cost/entry-filter
    semantics) whose `run()` strips news columns from `rows` before
    simulating fills. See the module docstring for why this alone is
    **not** sufficient for a genuine ablation — `rows` must already be
    news-free by the time a decision function scores them, which happens
    upstream of `run()` and is the caller's responsibility."""

    def run(self, rows: "list[dict]", decisions: "list[dict]", *, horizon_s: int) -> "list[dict]":
        return super().run(strip_news_columns(rows), decisions, horizon_s=horizon_s)
