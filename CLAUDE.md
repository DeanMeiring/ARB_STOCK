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
