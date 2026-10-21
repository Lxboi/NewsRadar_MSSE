# Paper Trading Methodology (Phase 15)

How to actually run a PAPER-mode test of the robot end to end, on your
own machine, and how to judge whether the result is worth anything before
revisiting Phase 14's live-Kraken gate. This is a process document, not
an implementation doc — see `docs/20_live_paper_trading.md` for the "how
it's built" side.

## 0. What "PAPER mode" actually does — and doesn't — do

PAPER mode runs the full real pipeline — live Hyperliquid order book →
`FeatureEngine` → your trained model → `SignalEngine` → `StrategyEngine`
→ `RiskManager` → `PaperExecutor` → `PerformanceTracker` — against real,
live market data, with **simulated money and simulated fills**
(`PaperExecutor`, Phase 13: sampled latency, realistic slippage/fee
assumptions, reject/partial-fill probability). No order ever reaches a
real exchange. Nothing about this phase touches Kraken or real capital —
that is still Phase 14's `KrakenExecutor`, still gated, still inert.

What this test can tell you: does the pipeline run stably for real,
unattended, for hours/days at a time; does a trained model's signal
actually translate into trade decisions and simulated PnL that make
sense; are there integration bugs that only show up against real,
messy, live data (as opposed to the clean synthetic data every unit
test uses). What it **cannot** tell you: whether the strategy is
profitable after real slippage/fees/latency on a real exchange, whether
it survives real adverse selection from other traders reacting to your
own orders, or anything about capacity/market impact. A clean paper
run is a necessary, not sufficient, condition for even considering
Phase 14.

## 1. Prerequisites

1. **Enough collected history to train a model.** You need a
   `features_v1` Parquet table (`scripts/build_feature_table.py`'s
   output) covering at least a few days of real market data per asset
   you want to trade. If you haven't been running `python -m src.main`
   in RESEARCH mode to collect raw data yet, start there first — this
   phase adds nothing to data collection, it only consumes what's
   already been collected.
2. A Python environment with this project's `requirements.txt`
   installed (`joblib` is a new dependency this phase added, via
   `scikit-learn`'s own transitive dependency — already covered by the
   existing `scikit-learn>=1.6` pin, no new line needed in
   `requirements.txt`).
3. `.env` present (copy from `.env.example` if you haven't already) —
   `PAPER_TRADING` is the flag this whole methodology revolves around.

## 2. Build the feature table

From the project root, with your venv active:

```
python -m scripts.build_feature_table --asset BTC --start 2026-08-01 --end 2026-08-18
```

Repeat per asset you want to paper-trade (BTC/ETH/SOL, or whichever
subset is `enabled: true` in `config/assets.yaml`). This reads the raw
Parquet ticks + `news_events` SQLite table Phases 2-5 have been
collecting and writes `data/features/features_v1/asset=<X>/date=<Y>/`.

## 3. Train and evaluate a candidate model — without promoting it yet

```
python -m scripts.train_model --asset BTC --start 2026-08-01 --end 2026-08-18 \
    --horizon-s 30 --threshold-bps 10
```

`--horizon-s`/`--threshold-bps` must be one of `config/strategy.yaml:
horizons_s`/`move_thresholds_bps` — `30`/`10` is a reasonable starting
point (a 30-second look-ahead, a 10bps move threshold), not a claim
that it's the best one. This step:

- Splits your data chronologically (never randomly), purged by
  `horizon_s` seconds at the boundary, into training and validation.
- Fits a `LogisticDirectionModel` (the default; pass `--model tree` for
  `TreeDirectionModel`) on the training split.
- Evaluates on the validation split: directional accuracy vs. a coin
  flip (a one-sided binomial test — read `beats_coin_flip` in the log
  output; if it's `False`, this candidate has no statistically
  detectable edge on this data and isn't worth promoting) and
  calibration diagnostics (Brier score, ROC-AUC).
- Saves the fitted model and registers it in `data/models/registry.jsonl`
  — but **does not promote it**. It exists, it's evaluated, but
  `PaperTradingEngine` will not load it yet.

Look at the logged `accuracy`/`p_value`/`beats_coin_flip` and
`roc_auc_macro`/`brier_score` before doing anything else. A model that
doesn't beat a coin flip on direction, or that's badly miscalibrated
(`roc_auc_macro` near 0.5, `brier_score` far from what a well-calibrated
binary/three-class model should produce), is not ready to paper-trade —
retrain with more data, a different horizon/threshold, `--calibrate`
(Platt scaling on the validation split), or `--model tree` before moving
on.

**This step is where real judgment belongs, not automation** — nothing
in this pipeline decides "good enough" for you, by design (section 59:
no model is ever auto-promoted).

## 4. Promote the model you've decided to trust

Once you've reviewed a candidate's numbers and decided it's worth
running live-paper:

```
python -m scripts.train_model --asset BTC --start 2026-08-01 --end 2026-08-18 \
    --horizon-s 30 --threshold-bps 10 \
    --promote --reason "first paper-trading candidate, beats coin flip p<0.01, 18 days history"
```

(Or promote an already-registered candidate separately — see
`ModelRegistry.promote()` if you want to script this without
retraining.) `--reason` is required and becomes part of the permanent
audit trail in `registry.jsonl` — write something you'd want to read
back in three months.

Repeat for every asset you want the live run to trade. **An asset with
no promoted model is skipped, not fatal** — `main.py` logs which
assets got skipped at startup and keeps running the rest.

## 5. Configure PAPER mode

Two independent flags, both required (section 65's two-gate discipline,
now enforced in code — see `docs/20_live_paper_trading.md`):

1. In `config/settings.yaml`, set `trading_mode.default: "PAPER"`.
2. In `.env`, set `PAPER_TRADING=true`.

Review `config/settings.yaml`'s `paper_trading:` block before starting —
in particular `horizon_s`/`threshold_bps` must match exactly what you
promoted in step 4 (a mismatch means `PaperTradingEngine` will find no
promoted model and skip every asset — check the startup log for
`PAPER TRADING: no promoted model yet for [...]` if that happens).
`tick_interval_s` (how often the strategy is evaluated), `initial_cash`/
`account_equity` (the simulated starting balance), and
`performance_snapshot_interval_s` (how often a PnL snapshot is written
to SQLite) are all yours to tune.

## 6. Run it

```
python -m src.main
```

You should see, alongside the existing Phase 2-5 console output:

- A startup log line confirming `PAPER TRADING ENABLED` and which
  assets are actually trading (vs. skipped for lack of a promoted
  model).
- Every `tick_interval_s`, the strategy is evaluated for each trading
  asset — you won't see a log line per tick (that would be noise), but
  every `POSITION_OPENED`/`POSITION_CLOSED`/`HIGH_CONVICTION_SIGNAL`/
  `RISK_ALERT` is logged (Telegram delivery itself is not wired this
  phase — see `docs/20_live_paper_trading.md`'s "Telegram" section —
  so these alerts currently only reach the console log, not your
  phone).
- A `paper trading snapshot` panel roughly every 15 seconds: open
  positions, the latest signal per asset, and all-time win rate/PnL/
  drawdown — the same kind of at-a-glance panel `live_display()`/
  `news_live_display()` already give you for market data and news.

Let it run. Stop it any time with Ctrl+C (or SIGTERM) — shutdown is
clean, matching every other phase's task-cancellation behavior.

## 7. Monitor while it runs

Three ways to check on it, in increasing order of detail:

1. **The console log.** Fastest, lowest-effort — the paper trading
   panel described above.
2. **Query SQLite directly.** `data/processed/crypto_radar.db`'s
   `positions` table has one row per closed trade (entry/exit price,
   side, PnL, fees, slippage, exit reason) and `performance` has a
   periodic all-time snapshot — both new this phase (`storage/
   database.py: SQLiteRecorder.write_position()`/
   `write_performance_snapshot()`). A simple `sqlite3
   data/processed/crypto_radar.db "select * from positions order by
   closed_at desc limit 20"` gives you the most recent trades without
   touching Python.
3. **`data/models/registry.jsonl`** is your audit trail if you ever
   need to answer "which exact model, trained on which exact data, made
   this decision" — every promoted model's full training period,
   features, and validation performance is there.

## 7b. Zero trades after hours of running? Check the probability floor first

Before assuming the strategy simply found nothing to trade, check
`/signals` (or query a live signal directly): if `probability_up`/
`probability_down` never gets anywhere near `entry_filter.
probability_up_min` (0.62 by default), the model isn't silently
declining to trade -- it's being rejected by a threshold that was never
calibrated against *this* model. `strategy.yaml`'s `entry_filter`/
`signal_engine` probability floors are generic Phase 11 starting points
(the file says so directly: "must be re-optimized via walk-forward"),
set before any real model existed. A well-calibrated model can have a
top-decile confidence well under 0.62 and still carry a statistically
real directional edge (`scripts/train_model.py`'s own `beats_coin_flip`
check doesn't require any probability floor at all -- it scores the
argmax direction regardless of magnitude).

`scripts/sweep_entry_threshold.py` answers "what would trading at a
lower probability floor actually have looked like" without touching any
config: it reuses the exact same `decisions_from_model()`/
`EventBacktester` entry-filter logic `should_enter()` runs live, swept
across several `probability_min` values over an already-built
`features_v1` range, and prints trade count/win rate/net PnL/expectancy/
p-value per threshold.

    python -m scripts.sweep_entry_threshold --asset BTC \
        --start 2026-08-21 --end 2026-08-24 \
        --horizon-s 60 --threshold-bps 10

As with any threshold search: picking the best-looking value out of
several tried is itself a form of multiple comparisons (same caution as
picking `(horizon_s, threshold_bps)` in step 3) -- use this to find a
*range* worth re-validating on fresh data, not to lock in whichever
single number happened to look best on one sweep.

## 8. How long to run before drawing conclusions

There's no single correct number — it depends entirely on how many
trades your strategy actually generates (a tight `entry_filter` might
produce only a handful of trades a day). As a floor, not a target:

- **A few hours minimum** just to confirm operational stability — no
  crashes, no silently-stuck tasks, sensible-looking signals and
  position sizing, before trusting the run at all.
- **Enough trades for the same statistical bar `scripts/train_model.py`
  already applies to validation accuracy** — a binomial test needs a
  reasonable sample size before "beats a coin flip" means anything.
  Tens of trades is a bare minimum to even look at a win rate; treat
  anything below that as "too early to tell," not as a result.
- **Multiple market regimes**, ideally — a run that only ever saw
  `LOW_VOL`/`NORMAL_VOL` (per `market_regime`) tells you nothing about
  behavior in `HIGH_VOL`/`EXTREME_VOL`, and `entry_filter.
  allowed_regimes` in `strategy.yaml` already excludes those by
  default — worth deliberately reviewing whether that's still the
  right call once you have real regime-labeled paper-trading history.
- **At least the same order of magnitude of days as `--start`/`--end`
  covered in training**, as a sanity floor against pure overfitting to
  the training window's specific conditions — a model trained on 18
  days that only gets paper-tested for 2 hours hasn't been tested
  against much that's actually new.

## 9. What to look for before revisiting the Phase 14 live-Kraken gate

Phase 14's `KrakenExecutor` exists, fully built and tested, but
deliberately inert (three independent gates, all off by default — see
`docs/19_kraken_executor.md`). Section 5 of the master prompt is
explicit that live trading is gated on paper-trading validation, not on
phase numbering. Before even considering flipping any of
`KrakenExecutor`'s three gates, a paper-trading run should show:

- Positive net PnL after simulated fees/slippage (`PerformanceTracker`'s
  `net_pnl`, not `gross_pnl` — the whole point of `PaperExecutor`'s cost
  realism is to not fool yourself with a gross number).
- A win rate and expectancy consistent with what `scripts/train_model.py`'s
  validation step predicted — if paper-trading performance is
  materially worse than validation suggested, that's a signal of
  overfitting or regime drift, not something to trade through.
- No operational surprises — no repeated `RISK_ALERT`/kill-switch
  engagements from `max_consecutive_losses`/`max_daily_loss_pct`
  (`strategy.yaml: risk`), no crashes, no silently-skipped assets you
  didn't intend to skip.
- A sample size and duration you're genuinely willing to defend as
  meaningful, per section 8 above — not just "it happened not to lose
  money in the two hours I watched it."

None of this is automated, deliberately — the same "no model is
auto-promoted" discipline `ModelRegistry.promote()` enforces in code
applies one level up here too: no amount of paper-trading data
automatically flips Kraken's gates. That decision, like every
capital-risk decision in this project, is the user's to make explicitly,
informed by what this methodology helps you actually look at.

## 10. Resetting between test runs

There's no dedicated "reset" command this phase — if you want a clean
slate for a new paper-trading experiment (a different model, a
different asset set, a fresh equity curve):

- `PerformanceTracker`/`RiskManager` state lives only in the running
  process's memory — simply stopping and restarting `python -m
  src.main` starts both fresh (equity resets to `paper_trading.
  initial_cash`/`account_equity`, the daily-loss/consecutive-loss
  counters reset).
- Nothing is deleted from SQLite (`positions`/`performance`) or
  `registry.jsonl` between runs — every run's history accumulates
  there, on purpose (an audit trail, per section 59's discipline
  extended to this phase). If you want to compare test A vs. test B
  cleanly, filter by timestamp when you query, rather than wiping the
  database.
- To swap which model is live without touching the database, promote a
  different `model_version` for the same `(asset, horizon_s,
  threshold_bps)` key (`ModelRegistry.current_promoted()` always
  resolves to whichever promotion happened most recently) and restart
  `main.py`.
