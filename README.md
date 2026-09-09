# ARB_STOCK

Arbitrage monitor with two independent detectors:
1. **Triangular** - loops USDT -> BTC -> ETH -> USDT (and the reverse) on
   Binance alone, over its live WebSocket book-ticker stream.
2. **Cross-exchange** - compares Binance's BTCUSDT against Crypto.com's
   BTC_USDT, polled over REST.

Both log any opportunity that clears the fee-adjusted `MIN_PROFIT_THRESHOLD`
to Postgres, and message anyone who has `/login`'d via Telegram.

**Right now: `config.EXECUTE_TRADES` is off, so it's detection and Telegram
alerts only.** Message the bot `/login <password>` (see
`TELEGRAM_LOGIN_PASSWORD`) to receive an alert whenever a real opportunity
clears threshold, then execute manually if you want to act on it.
`executor.py` implements real execution for the **triangular** loop only
(not cross-exchange), gated behind `EXECUTE_TRADES` and, before every single
order, `governor.py`'s safety checks - see **Live trading** below.
`BINANCE_API_KEY`/`BINANCE_API_SECRET` are now a real, IP-restricted,
trade-permission key (routed through `BINANCE_PROXY_URL`'s static IP), and
`BINANCE_BASE_URL` points at real Binance (`https://api.binance.com`), not
Testnet - `EXECUTE_TRADES` is the only thing still holding this at
detection-only, and it stays `False` until that's a deliberate, separate
decision.

## Why detection-only for now

`EXECUTE_TRADES=False` - everything below it (real key, real
`BINANCE_BASE_URL`, static-IP proxy, governor) is wired up and exercisable,
but no order gets placed until that flag is flipped on purpose.

## Telegram commands

- `/login <password>` - subscribe to opportunity alerts
- `/report` - summary stats for both detectors, plus an inline price-trend prediction prompt
- `/predict` - price-trend prediction across all `config.PREDICT_SYMBOLS` coins (`price_predictor.py`, trained daily)
- `/trainstatus` - outcome of the last daily training cron run
- `/tradestatus` - governor state: kill switch, today's/cumulative P&L, trade count vs. the configured limits
- `/halt <reason>` / `/resume` - manually stop/resume live execution

`/predict` also appends today's prediction-accuracy tally and hypothetical
P&L - see **Prediction accuracy tracking** below.

## Models

`analyze_opportunity_model.py` and `analyze_price_trend_model.py` run daily
(Railway cron, see the `opportunity-model` service) training XGBoost models
on the accumulated `opportunities`/`near_misses`/`market_candles` history,
saved to Postgres (`trained_models` - Railway's filesystem doesn't persist
across deploys).

Every training run tops each `config.PREDICT_SYMBOLS` coin's candle history
up to a full `BACKFILL_DAYS` (90 days of 1-minute candles) via Binance's
historical klines endpoint before training - not just the first time a coin
is added, but every run, so an existing coin catches up too if `BACKFILL_DAYS`
gets raised later (`analyze_price_trend_model.ensure_backfilled`). This is
what gives the model more history to learn from without you remembering to
run anything manually. `backfill_candles.py` remains as a standalone script
if you want to run the same backfill outside a training pass (e.g. to seed
a coin immediately after adding it, without waiting for the next cron run).

Deeper history does mean more Postgres storage for `market_candles` - 90
days x 1-minute candles x 7 coins is roughly 900k rows, a moderate but not
huge footprint. If storage becomes a concern, `BACKFILL_DAYS` in
`backfill_candles.py` is the one knob to turn down.

**Features** (`analyze_price_trend_model.FEATURE_COLS`): return over the
last 1/5/15 candles, 15-candle return volatility, hour/day-of-week
(cyclical), `volume_ratio` (this candle's traded volume vs its own trailing
15-candle average - scale-invariant, so comparable across coins with very
different raw volume), and `btc_return_5` (BTCUSDT's own 5-candle return at
the same timestamp, on the idea that BTC often moves first and alts follow
a few minutes later). Live volume comes from a second WebSocket connection
(`binance_client.BinanceKlineVolumeStream`, kline_1m) - `bookTicker` (the
feed everything else uses) carries no trade volume at all, only price.

`market_candles` rows written before the `volume` column existed have it
NULL; `ensure_backfilled` detects that and re-fetches the full
`BACKFILL_DAYS` window once per coin to patch it in (idempotent - only
overwrites a NULL, via `bulk_log_candles`'s `ON CONFLICT ... DO UPDATE`),
so this happens automatically on the next training run rather than needing
a separate migration - but that one-time full re-fetch (unlike the usual
gap-only top-up) takes noticeably longer, so don't be surprised if a
training run right after this shipped takes longer than usual.

## Paper trading (prediction accuracy + hypothetical P&L)

Separately from the daily-retrained models above, `prediction_tracker.py`
simulates one long position per coin, driven entirely by the model's own
confidence rather than a fixed clock - prices (and the model's confidence)
can move fast enough that a fixed hold time either exits too early or holds
too long. Every `config.PREDICTION_CHECK_INTERVAL_MINUTES` (60, i.e. hourly -
raised from an initial 1-minute check after that turned out to open/close
positions too fast for the price move to clear the round-trip fee cost even
on directionally-correct calls), for each `config.PREDICT_SYMBOLS` coin, it
re-checks the current `prob_up` against `config.PREDICT_UP_THRESHOLD` (the
same 0.65 `/predict` already highlights coins at) and reacts to a crossing.
The model itself now matches that cadence too:
`analyze_price_trend_model.HORIZON_CANDLES` (60) means `prob_up` is trained
to answer "higher an hour from now?", not "higher next minute?" like it
originally did - the two were mismatched (a fast-moving 1-minute signal
being checked hourly) until this was raised to match.

- **No open position, prob_up crosses >= threshold** -> "buys": opens a
  paper position (`paper_trades`) at the current price.
- **Open position, prob_up drops back below threshold** -> "sells": closes
  it, scoring the hypothetical P&L of having held `config.TRADE_SIZE_USDT`
  of it for however long the model stayed confident, fees included both ways.
- Otherwise (still confident and already holding, or still unconfident and
  not holding) - does nothing that tick.

This never places a real order and is completely independent of
`EXECUTE_TRADES`/`executor.py`/`governor.py` - it's purely a measurement of
whether this exact strategy would have made money before you'd ever trust it
with real money. Today's tally (SAST) shows up in `/predict` and on the
dashboard as "Paper-trade win rate (today)" / "Hypothetical P&L (today)" -
a "win" means the closed trade was profitable after fees, not just
directionally correct.

## Web dashboard

`web.py` + `web/dashboard.html` serve a live dashboard on the same
process/port Railway routes traffic to (`$PORT`, `config.DASHBOARD_PORT`,
default 8080 locally) - no separate service needed. It's interactive: a
time-range selector (1H/6H/24H/3D/7D) drives the time-windowed charts, a
coin dropdown picks which symbol the single-coin price chart shows, and a
manual refresh button/auto-refresh toggle sit alongside the usual 30s
auto-refresh. All timestamps display in SAST (UTC+2), not UTC.

It shows stat tiles for both detectors plus today's paper-trade win rate and
hypothetical P&L, a multi-coin signal chart (every tracked coin's price
normalized to % change so wildly different price scales are comparable,
colored/labeled by its latest up/down call), a single-coin price chart -
overlaid with ▲/▼ markers for every paper trade opened/closed on that coin
in the selected window, plus a list below it spelling out each trade's
entry/exit and P&L (or "still open") - a near-miss trend chart with the
threshold marked, the price-trend prediction bars, a table of trained
models, and the opportunities profit-% chart - all read live from the same
Postgres tables the bot and cron jobs already write to, nothing extra
stored for it.

Set `DASHBOARD_PASSWORD` before this is exposed on a public URL - Railway
will generate one once you enable networking for this service, and without
a password anyone with the link can see your live data. Basic Auth,
username `admin`, password from that env var.

## Live trading

`executor.py` places real market orders for the triangular loop (not
cross-exchange) when `EXECUTE_TRADES=True`, against whichever
`BINANCE_BASE_URL` points to. Every attempt goes through `governor.py`
first, which blocks the trade if any of:

- **Kill switch is on** - manual (`/halt` / `/resume`) or auto-triggered by
  the two conditions below (never auto-clears - needs a human).
- **Cumulative loss across all trades ever** >= `MANUAL_REVIEW_LOSS_THRESHOLD_USDT`.
- **Any trade with unknown P&L** - a leg-3 failure unwinds through ETHBTC,
  not back to USDT, so the resulting P&L isn't cleanly computable; treated
  as exactly as dangerous as a big loss, since the account may be sitting
  in an untracked position.
- **Today's realized loss** >= `MAX_DAILY_LOSS_USDT` (auto-lifts at UTC midnight).
- **Today's trade count** >= `MAX_TRADES_PER_DAY` (same auto-lift).
- **Less than `MIN_SECONDS_BETWEEN_TRADES`** since the last attempt.

All of the above persist in Postgres (`trades`, `governor_state`), so they
survive redeploys - `/tradestatus` reads them live and works even with
`EXECUTE_TRADES` off, so the whole governor path is exercisable against
Testnet before anything real is at stake.

**Done:** static-IP proxy (`BINANCE_PROXY_URL`), IP-restricted trade-permission
key (`BINANCE_API_KEY`/`BINANCE_API_SECRET`), and `BINANCE_BASE_URL` pointed
at real Binance are all live. **Not done:** `EXECUTE_TRADES` is still
`False` - flip it only after separately deciding you're ready, ideally after
watching `/tradestatus` and a read-only balance check succeed against the
real account first.

## Plan

1. Run detection-only for a while, accumulate real opportunity data.
2. Review how often/how large real opportunities actually are (see the
   `opportunities` table in Postgres) before sizing up past the $25 test
   plan (`TOTAL_LIVE_CAPITAL_USDT` etc. in `config.py`).

## Run locally

```
pip install -r requirements.txt
export DATABASE_URL=postgresql://...
export TELEGRAM_API_BOT=...
export TELEGRAM_LOGIN_PASSWORD=...
export DASHBOARD_PASSWORD=...
# optional, only for live trading:
export BINANCE_API_KEY=...
export BINANCE_API_SECRET=...
export BINANCE_PROXY_URL=...
python main.py
# dashboard at http://localhost:8080
```
