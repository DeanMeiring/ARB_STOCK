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

## Models

`analyze_opportunity_model.py` and `analyze_price_trend_model.py` run daily
(Railway cron, see the `opportunity-model` service) training XGBoost models
on the accumulated `opportunities`/`near_misses`/`market_candles` history,
saved to Postgres (`trained_models` - Railway's filesystem doesn't persist
across deploys). `backfill_candles.py` is a one-off script to seed
`market_candles` with deeper history than live collection alone would have.

## Web dashboard

`web.py` + `web/dashboard.html` serve a live dashboard on the same
process/port Railway routes traffic to (`$PORT`, `config.DASHBOARD_PORT`,
default 8080 locally) - no separate service needed. It shows stat tiles for
both detectors, an opportunities profit-% chart, a BTCUSDT price chart, a
near-miss trend chart with the threshold marked, and a table of trained
models - all read live from the same Postgres tables the bot and cron jobs
already write to, nothing extra stored for it.

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
