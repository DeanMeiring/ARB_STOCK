# ARB_STOCK

Arbitrage monitor with two independent detectors:
1. **Triangular** - loops USDT -> BTC -> ETH -> USDT (and the reverse) on
   Binance alone, over its live WebSocket book-ticker stream.
2. **Cross-exchange** - compares Binance's BTCUSDT against Crypto.com's
   BTC_USDT, polled over REST.

Both log any opportunity that clears the fee-adjusted `MIN_PROFIT_THRESHOLD`
to Postgres, and message anyone who has `/login`'d via Telegram.

**Right now: detection and Telegram alerts only, no automated execution.**
Message the bot `/login <password>` (see `TELEGRAM_LOGIN_PASSWORD`) to
receive an alert whenever a real opportunity clears threshold, then execute
manually if you want to act on it. `config.EXECUTE_TRADES` gates an existing
Testnet execution path (`executor.py`) but that stays off for now - the
Binance API key currently in use is Reading-only and cannot place orders.

## Why detection-only for now

Binance requires trading-permission API keys to be IP-restricted, which
needs a static outbound IP (a Railway Pro feature). Until that's worth
paying for, opportunities get logged and alerted, not auto-executed.

## Telegram commands

- `/login <password>` - subscribe to opportunity alerts
- `/report` - summary stats for both detectors, plus an inline BTC prediction prompt
- `/predict` - BTC price-trend prediction directly (`price_predictor.py`, trained daily)
- `/trainstatus` - outcome of the last daily training cron run

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

## Plan

1. Run detection-only for a while, accumulate real opportunity data.
2. Review how often/how large real opportunities actually are (see the
   `opportunities` table in Postgres) and decide whether automated
   execution (Railway Pro + IP-restricted trading key + governors -
   kill switch, daily loss limit, rate limiter) is worth building.

## Run locally

```
pip install -r requirements.txt
export DATABASE_URL=postgresql://...
export TELEGRAM_API_BOT=...
export TELEGRAM_LOGIN_PASSWORD=...
export DASHBOARD_PASSWORD=...
python main.py
# dashboard at http://localhost:8080
```
