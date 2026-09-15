# ARB_STOCK

A triangular arbitrage monitor for Binance. It watches the live order book
for three pairs that form a loop (USDT -> BTC -> ETH -> USDT, and the same
loop run in reverse), recalculates the fee-adjusted theoretical return of
that loop on every price tick using real bid/ask prices, and logs any
opportunity that clears a configurable profit threshold to Postgres, with a
Telegram alert to anyone subscribed. It's a personal project for learning
the mechanics of arbitrage detection and building a real, if small, trading
data pipeline - not a packaged product.

**Right now: detection and Telegram alerts only, no automated execution.**
Message the bot `/login <password>` (see `TELEGRAM_LOGIN_PASSWORD`) to
receive an alert whenever a real opportunity clears threshold, then execute
manually if you want to act on it. `config.EXECUTE_TRADES` gates an existing
Testnet execution path (`executor.py`) but that stays off for now - the
Binance API key currently in use is Reading-only and cannot place orders.

## Features

- **Real-time detection** - subscribes to Binance's combined WebSocket
  book-ticker stream for `BTCUSDT`, `ETHBTC`, and `ETHUSDT`, and reconnects
  automatically on any drop.
- **Both loop directions** - checks the forward (USDT->BTC->ETH->USDT) and
  reverse (USDT->ETH->BTC->USDT) loop on every tick, using best bid/ask
  (never mid-price) so the numbers reflect what you could actually trade at.
- **Fee-aware math** - profit is calculated net of the configured taker fee
  per leg (`config.TAKER_FEE`), against a configurable minimum threshold
  (`config.MIN_PROFIT_THRESHOLD`) before anything is logged or alerted.
- **Persistent logging** - every qualifying opportunity is written to
  Postgres (`opportunities` table) with full bid/ask context, so opportunity
  frequency and size can be analyzed over time.
- **Telegram bot** - `/login <password>` subscribes a chat to opportunity
  alerts; `/report` replies with summary stats (totals, most recent, best
  ever, subscriber count) pulled straight from Postgres.
- **Optional Testnet execution path** (`executor.py`, off by default via
  `config.EXECUTE_TRADES`) - places the three legs as sequential Testnet
  market orders and attempts to unwind (flatten the position) if a later leg
  fails partway through the loop.

## Tech stack

- Python 3, `asyncio` for concurrent WebSocket streaming + Telegram polling
- `websockets` for the Binance book-ticker stream
- `requests` for the Binance REST (order execution) and Telegram Bot APIs
- `psycopg2` / PostgreSQL for opportunity and subscriber storage
- Deployed on [Railway](https://railway.app), including its managed
  Postgres add-on

## Why detection-only for now

Binance requires trading-permission API keys to be IP-restricted, which
needs a static outbound IP (a Railway Pro feature). Until that's worth
paying for, opportunities get logged and alerted, not auto-executed.

## Plan

1. Run detection-only for a while, accumulate real opportunity data.
2. Review how often/how large real opportunities actually are (see the
   `opportunities` table in Postgres) and decide whether automated
   execution (Railway Pro + IP-restricted trading key + governors -
   kill switch, daily loss limit, rate limiter) is worth building.
3. Separately, once enough historical price data has accumulated, evaluate
   whether a predictive model (e.g. XGBoost) is worth adding alongside the
   deterministic detection logic above.

## Run locally

```
pip install -r requirements.txt
python main.py
```

Configuration is via environment variables (see `config.py` for defaults
and full context) - never commit real values for these:

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | Yes | Postgres connection string for opportunity/subscriber logging |
| `TELEGRAM_API_BOT` | Yes, for alerts | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_LOGIN_PASSWORD` | Yes, for alerts | Password required for `/login` to subscribe a chat |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Only if `EXECUTE_TRADES=True` | Binance (Testnet) API credentials for order execution |

Without `DATABASE_URL` and the Telegram vars set, the app still runs and
prints detected opportunities to stdout - it just won't persist or alert on
them.
