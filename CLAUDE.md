# Working style for this project

The user has a bachelor's in computer science and is currently applying for
ML/AI jobs. This project (crypto arb bot + ML price prediction, and the
stocks version being built alongside it) is deliberately also a hands-on
way to relearn and deepen the actual math and mechanics behind it - not
just a thing to be built and handed over finished.

**Before implementing anything non-trivial** (a new feature, a new
pipeline, a new data source, an architectural choice) - explain the plan
first: what the pipeline looks like end-to-end, what the tradeoffs are,
why one approach was picked over another. Let the user weigh in or just
understand it before it's already built, not only after. Small, obvious
fixes (a bug fix, a one-line config change) don't need this ceremony -
it's for anything where the "how" or "why" is worth walking through.

**Default toward more technical/mathematical depth, not less**, especially
for the ML pieces (XGBoost, gradient boosting, feature engineering,
evaluation metrics, etc.) - explain the actual math, not just a hand-wavy
summary. Concrete worked examples (small numbers, a tiny table, walked
through step by step - this earlier session had good results with a
literal "grab your paper" pen-and-math approach) land better than
equations alone. The user will explicitly say when something is too much
detail - so it's fine, even preferred, to err on the side of thorough
rather than trimming preemptively.

**Confirm before committing/pushing.** The user has been consistently
deliberate about controlling exactly when git pushes/redeploys happen
(a redeploy affects live state - open paper trades, in-memory schedule
timers, etc.). Build and test locally, report what's ready, and wait for
an explicit go-ahead before `git commit`/`git push`, unless told otherwise
for a specific stretch of work.

## Project shape (brief)

- `main.py` - single asyncio process: Binance price/volume WS streams,
  Telegram polling, the dashboard (FastAPI), paper-trading loop, all in
  one `asyncio.gather`.
- `arb_calculator.py` - triangular (Binance) and cross-exchange (Binance vs
  Crypto.com) arbitrage math, using bid/ask (never mid-price).
- `analyze_price_trend_model.py` / `price_predictor.py` - XGBoost
  classifier per coin (config.PREDICT_SYMBOLS), predicting probability of
  price being higher 1 hour ahead. Trains via a daily Railway cron
  (`opportunity-model` service).
- `prediction_tracker.py` - paper trading on the model's own signal: buys
  when prob_up crosses config.PREDICT_UP_THRESHOLD, sells on the threshold
  dropping back below it, or on the stop-loss/take-profit circuit breakers
  (config.STOP_LOSS_PCT / TAKE_PROFIT_NET_PCT).
- `logger.py` - all Postgres reads/writes, one function per table/query.
- `web.py` + `web/dashboard.html` - the live dashboard (FastAPI + Chart.js,
  vanilla JS, no build step).
- Deploys on Railway (project "grand-purpose"): `ARB_STOCK` (the app),
  `opportunity-model` (daily training cron), `Postgres`.
- Push branches: `claude/repo-review-8yrk3f` and `claude/code-review-gk7z80`
  (both get every push).

A stocks-market parallel version of this same system (own dashboard, own
data source, own pipeline - not intermixed with the crypto tables) is
being planned/built alongside this - see recent conversation history for
the design discussion.

A second, separate session (not this one) added, in parallel: a
cross-sectional/relative-strength model (does-this-coin-beat-the-basket,
alongside the original absolute up/down one - see
build_features_relative/predict_symbol_relative, `strategy` params
throughout, and `web/relative_dashboard.html` at `/relative`), purged
walk-forward CV with bootstrapped AUC confidence intervals (the honest
"is this a real edge or noise" check - `auc_ci_low`/`auc_ci_high`/
`auc_significant` in trained_models metadata), and order-book
imbalance/spread collection (not yet in FEATURE_COLS - see
`market_candles.avg_imbalance`/`avg_spread_bps`, no backfill path exists
for these). An off-switch/timed-sleep for paper trading also exists
(`/pause`, `/pause <duration>`, `/unpause` in Telegram; a dashboard tile)
- one shared switch that freezes BOTH strategies at once, separate from
the pre-existing `/halt` kill switch (which only ever gated real-money
execution, never used on this project).

## Findings as of 2026-09-21 - read this before assuming either model needs more work

Checked the actual bootstrapped AUC + 95% CI (not just win-rate %) across
4 consecutive days of retraining (Sept 17-20): **both the absolute and
relative models sit at "not distinguishable from chance" on 6 of 7
coins, every single day.** DOGEUSDT is the one coin with a borderline-real
signal in both strategies - but the absolute model's DOGEUSDT AUC has
been equal to or slightly HIGHER than the relative model's every day
that week (e.g. 0.542 vs 0.517 on the 20th). There is no evidence in the
rigorous metric that the relative/cross-sectional reframing is actually
better - if anything it leans the other way on the one coin either model
shows real signal on.

The dashboard's apparent 62% (relative, 13/21 trades) vs 55% (absolute,
49/89 trades) lifetime gross win-rate gap looked meaningful from the raw
percentage alone, but isn't: computed the actual binomial probability of
seeing results at least that extreme from a true 50/50 coin flip - about
19-20% for BOTH models. Small samples naturally swing further from 50%;
that gap is consistent with pure noise, not a real difference between
the two strategies. Don't reach for "the relative model looks more
promising" from win-rate percentages alone - check auc_significant/the
CI first, same as this session did.

Given no real edge is currently demonstrated on either model (except the
maybe-real, inconsistent DOGEUSDT signal) and to cut Railway costs, the
ARB_STOCK app was put into Railway sleep mode on 2026-09-21
(`sleepApplication: true` + redeploy) rather than deleted - it idles
after inactivity and wakes on the next request to its dashboard URL.
Everything (code, Postgres data, trained models) is untouched and ready
to resume. Note: since main.py runs the price streams, paper trading,
AND the Telegram bot all in one process, NOTHING responds while asleep -
not even Telegram commands - and simply visiting the dashboard is what
wakes it back up. If asked to bring it back to always-on, disable
sleepApplication via Railway's update-service and redeploy.

If/when picked back up: the highest-signal next step is investigating
DOGEUSDT specifically (why it's the one coin either model shows anything
on), not blindly investing more in the relative strategy - that's not
supported by the evidence gathered so far.
