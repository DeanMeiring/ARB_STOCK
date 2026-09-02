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
TAKER_FEE = 0.001

# --- Profit threshold ---
# Minimum theoretical profit (as a fraction, e.g. 0.001 = 0.1%) before we
# bother logging an opportunity. Real spreads this small will likely be gone
# before you could ever execute manually - this is for detection/logging,
# not live trading.
MIN_PROFIT_THRESHOLD = 0.0005  # 0.05%

# --- WebSocket ---
BINANCE_WS_BASE = "wss://stream.binance.com:9443/stream"

# --- Logging ---
DB_PATH = "logs/opportunities.db"
LOG_ALL_TICKS = False  # if True, logs every price update, not just opportunities (large file fast)


# --- Live/Testnet execution ---
# EXECUTE_TRADES is the master switch. False = detection/logging only (default,
# safe). True = actually places orders against whichever BASE_URL is set below.
EXECUTE_TRADES = False

# Binance Testnet - fake money, real order matching engine/API behavior.
# Get free testnet API keys at https://testnet.binance.vision/
# NEVER hardcode keys here - set them as environment variables:
#   export BINANCE_API_KEY="..."
#   export BINANCE_API_SECRET="..."
import os
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
BINANCE_BASE_URL = "https://testnet.binance.vision"  # DO NOT point this at api.binance.com without a full review

# Amount of USDT to risk per triangular loop attempt (testnet money)
TRADE_SIZE_USDT = 20.0

# --- Starting capital assumption (for logging theoretical $ profit only) ---
# Matches TRADE_SIZE_USDT so logged opportunity $ amounts reflect what a real
# loop attempt would actually make/lose, not a placeholder portfolio size.
SIMULATED_START_USDT = 20.0

# --- Live-capital test plan (numbers only - NOT YET ENFORCED) ---
# Agreed sizing for the first small live-capital test: $25 total funded to the
# exchange account, trading with it directly rather than holding most of it in
# reserve. These constants exist so the governor code (kill switch, daily loss
# limit, rate limiter - none of which exist yet) has agreed values to wire up
# against. Defining them here does NOT turn on live trading by itself -
# EXECUTE_TRADES and BINANCE_BASE_URL above are what gate that, and neither
# should change until the governors that read these values are built and
# tested.
TOTAL_LIVE_CAPITAL_USDT = 25.0       # total funded to the exchange account
MAX_DAILY_LOSS_USDT = 3.0            # ~12% of capital - auto-halt for the day
MANUAL_REVIEW_LOSS_THRESHOLD_USDT = 5.0  # ~20% cumulative - no auto-reset past this, needs a human look
MAX_TRADES_PER_DAY = 10              # runaway-loop breaker, not a real constraint at this scale
MIN_SECONDS_BETWEEN_TRADES = 5       # guards against a tick burst firing several trades at once