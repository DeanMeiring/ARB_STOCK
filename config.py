"""
Configuration for the triangular arbitrage monitor.

Everything here is tunable so you can test different fee tiers / thresholds
without touching the core logic.
"""

# --- Triangular path ---
# Loop: USDT -> BTC -> ETH -> USDT
# Binance symbol naming: BASEQUOTE, price = how much QUOTE per 1 BASE
LEG_1 = "BTCUSDT"   # buy BTC with USDT
LEG_2 = "ETHBTC"    # buy ETH with BTC
LEG_3 = "ETHUSDT"   # sell ETH for USDT

SYMBOLS = [LEG_1, LEG_2, LEG_3]

# --- Fees ---
# Binance retail spot taker fee is 0.1% (0.001) per trade as of writing.
# If you hold BNB for fee discount, retail effective rate is ~0.075% (0.00075).
# CHECK CURRENT RATES on Binance before assuming this - fee schedules change.
# Only set USE_BNB_FEE_DISCOUNT=true if you actually hold BNB AND have "Pay
# fees with BNB" enabled in Binance settings - this code has no way to verify
# either from here, it just changes what rate gets assumed in the math.
import os
USE_BNB_FEE_DISCOUNT = os.environ.get("USE_BNB_FEE_DISCOUNT", "false").strip().lower() == "true"
TAKER_FEE = 0.00075 if USE_BNB_FEE_DISCOUNT else 0.001

# --- Profit threshold ---
# Minimum theoretical profit (as a fraction, e.g. 0.001 = 0.1%) before we
# bother logging an opportunity. Real spreads this small will likely be gone
# before you could ever execute manually - this is for detection/logging,
# not live trading.
MIN_PROFIT_THRESHOLD = 0.0005  # 0.05%

# --- WebSocket ---
BINANCE_WS_BASE = "wss://stream.binance.com:9443/stream"

# --- Watchdog / heartbeat ---
# Alert if no price tick has arrived in this long - likely stuck or disconnected.
STALE_TICK_ALERT_SECONDS = 600  # 10 minutes
# Separately, a "still running" Telegram message on this cadence regardless
# of anything being wrong - confirms the whole process (not just the WS) is alive.
HEARTBEAT_INTERVAL_SECONDS = 86400  # 24 hours

# --- Logging ---
DB_PATH = "logs/opportunities.db"  # legacy SQLite path, only used by executor.py's trades table
LOG_ALL_TICKS = False  # if True, logs every price update, not just opportunities (large file fast)

# Postgres (Railway add-on) - opportunities and Telegram subscribers live here now.
# Railway injects this automatically via the DATABASE_URL reference variable
# once the Postgres service is attached - never hardcode a connection string.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# --- Cross-exchange (Binance vs Crypto.com) ---
# Same BTC/USDT pair on a second venue - detection/logging only, same as the
# triangular side. No API key needed: this only reads Crypto.com's public
# ticker endpoint, nothing authenticated.
CRYPTOCOM_REST_BASE = "https://api.crypto.com/exchange/v1/public"
CRYPTOCOM_SYMBOL = "BTC_USDT"  # compared against Binance's BTCUSDT
# Crypto.com's standard retail taker fee is much higher than Binance's (~0.4%
# vs ~0.1%) - check your actual account tier, this varies a lot with volume/
# CRO stake and materially changes what counts as a real opportunity here.
CRYPTOCOM_TAKER_FEE = 0.0040
CRYPTOCOM_POLL_SECONDS = 2  # REST polling, not push-based like Binance's WS

# --- Telegram notifications ---
# Bot token from @BotFather. Anyone who messages the bot the right password
# via /login gets added as a notification recipient - see telegram_bot.py.
# .strip() guards against trailing whitespace/newlines from copy-pasting the
# value into Railway's variable field - a stray newline here silently broke
# every Telegram API call (404s) without a token error, since the token part
# was still valid, just the URL was malformed.
TELEGRAM_API_BOT = os.environ.get("TELEGRAM_API_BOT", "").strip()
TELEGRAM_LOGIN_PASSWORD = os.environ.get("TELEGRAM_LOGIN_PASSWORD", "").strip()


# --- Live/Testnet execution ---
# EXECUTE_TRADES is the master switch. False = detection/logging only (default,
# safe). True = actually places orders against whichever BASE_URL is set below.
EXECUTE_TRADES = False

# Real Binance - real money, real orders. Deliberately switched from Testnet
# on 2026-09-08 once the IP-restricted, trade-permission key and governor.py
# were in place. EXECUTE_TRADES (above) is what actually gates whether any
# order gets placed here - this alone does not turn on live trading, but do
# not treat that as a reason to be casual about this URL: every signed call
# through binance_rest.py now hits the real exchange, including balance
# checks, the moment EXECUTE_TRADES flips true.
# NEVER hardcode keys here - set them as environment variables:
#   export BINANCE_API_KEY="..."
#   export BINANCE_API_SECRET="..."
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "").strip()
BINANCE_BASE_URL = "https://api.binance.com"

# Static-IP proxy for the signed Binance calls in binance_rest.py (account
# balance, place order) - e.g. a QuotaGuard Static proxy URL
# ("http://user:pass@host:port"). Binance sees requests as coming from this
# proxy's fixed IP, which is what you whitelist on a trade-permission key
# (Binance requires those to be IP-restricted). Leave unset for Testnet/
# read-only use, where IP restriction doesn't apply - requests go out
# directly with Railway's normal (non-static) egress IP.
BINANCE_PROXY_URL = os.environ.get("BINANCE_PROXY_URL", "").strip()

# Amount of USDT to risk per triangular loop attempt (testnet money)
TRADE_SIZE_USDT = 20.0

# --- Starting capital assumption (for logging theoretical $ profit only) ---
# Matches TRADE_SIZE_USDT so logged opportunity $ amounts reflect what a real
# loop attempt would actually make/lose, not a placeholder portfolio size.
SIMULATED_START_USDT = 20.0

# --- Live-capital test plan ---
# Agreed sizing for the first small live-capital test: $25 total funded to the
# exchange account, trading with it directly rather than holding most of it in
# reserve. Enforced by governor.py (kill switch, daily loss limit, cumulative
# manual-review threshold, trade-count cap, min-gap-between-trades) - see that
# file's docstring. Defining/enforcing these does NOT turn on live trading by
# itself - EXECUTE_TRADES and BINANCE_BASE_URL above are what gate that.
TOTAL_LIVE_CAPITAL_USDT = 25.0       # total funded to the exchange account
MAX_DAILY_LOSS_USDT = 3.0            # ~12% of capital - auto-halt for the day
MANUAL_REVIEW_LOSS_THRESHOLD_USDT = 5.0  # ~20% cumulative - no auto-reset past this, needs a human look
MAX_TRADES_PER_DAY = 10              # runaway-loop breaker, not a real constraint at this scale
MIN_SECONDS_BETWEEN_TRADES = 5       # guards against a tick burst firing several trades at once

# --- Multi-coin price-trend prediction ---
# Extra USDT pairs (beyond the triangular loop's BTCUSDT/ETHUSDT) tracked
# purely for the price-trend model - subscribed on the same live WS stream
# and collected into market_candles alongside the triangular symbols, with
# no effect on arbitrage detection itself.
PREDICT_EXTRA_SYMBOLS = ["BNBUSDT", "SOLUSDT", "LINKUSDT", "INJUSDT", "DOGEUSDT"]
# Full set the price-trend model trains one classifier per symbol on.
PREDICT_SYMBOLS = ["BTCUSDT", "ETHUSDT"] + PREDICT_EXTRA_SYMBOLS
# /predict calls out coins scoring >= this as a probability-of-up highlight.
PREDICT_UP_THRESHOLD = 0.65

# --- Paper trading (simulated trades on the model's own signal, no real orders) ---
# prediction_tracker.py "buys" a coin (paper-only) the moment its prob_up
# crosses >= PREDICT_UP_THRESHOLD, and "sells" the moment it drops back
# below - a variable hold time driven entirely by the model's own confidence,
# not a fixed clock, since prices (and the model's confidence) can swing
# fast enough that a fixed window either exits too early or holds too long.
# Independent of EXECUTE_TRADES/governor.py entirely - never places a real
# order, only measures what this exact strategy would have made/lost.
#
# How often to re-check each coin's prediction and react to a threshold
# crossing - as often as the underlying data resolution allows (candles are
# 1-minute), so a crossing isn't missed for several minutes.
PREDICTION_CHECK_INTERVAL_MINUTES = 1

# --- Web dashboard ---
# Railway injects PORT automatically once a public domain is generated for
# this service; falls back to 8080 for local runs.
DASHBOARD_PORT = int(os.environ.get("PORT", 8080))

# Optional HTTP Basic Auth password protecting the dashboard and its API.
# Username is fixed as "admin". Leave unset only for local-only use - once a
# public domain exists (RAILWAY_PUBLIC_DOMAIN below), the dashboard is
# reachable by anyone with the link without this set.
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "").strip()

# Railway sets this automatically once a public domain is generated for this
# service (Settings -> Networking -> Generate Domain, or via the API) - empty
# until then, in which case DASHBOARD_URL below is also empty and the
# Telegram commands just omit the dashboard link.
RAILWAY_PUBLIC_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
DASHBOARD_URL = f"https://{RAILWAY_PUBLIC_DOMAIN}" if RAILWAY_PUBLIC_DOMAIN else ""