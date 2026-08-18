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

# --- Starting capital assumption (for logging theoretical $ profit only, no real trading) ---
SIMULATED_START_USDT = 1000.0