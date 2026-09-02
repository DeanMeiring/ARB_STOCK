# ARB_STOCK

Triangular arbitrage monitor for Binance (USDT -> BTC -> ETH -> USDT and the
reverse loop). Watches live bid/ask prices over the WebSocket book-ticker
stream, calculates the fee-adjusted theoretical return of the loop on every
tick, and logs any opportunity that clears `MIN_PROFIT_THRESHOLD` to Postgres.

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
export DATABASE_URL=postgresql://...
export TELEGRAM_API_BOT=...
export TELEGRAM_LOGIN_PASSWORD=...
python main.py
```
