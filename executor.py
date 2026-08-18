"""
Executes a triangular loop as 3 sequential MARKET orders against Binance
Testnet (or wherever config.BINANCE_BASE_URL points - default is Testnet,
and it should stay that way until you've reviewed this code very carefully).

IMPORTANT - read before enabling config.EXECUTE_TRADES:

- Orders are sent ONE AT A TIME, not atomically. Between the theoretical
  calculation and each order actually filling, prices move. The real
  profit/loss will differ from what the detector calculated - sometimes
  a lot.
- If leg 2 or 3 fails after leg 1 succeeded, this code attempts to unwind
  by selling back what it just bought, AT MARKET, immediately. That unwind
  itself costs a taker fee and slippage - a failed loop is a guaranteed
  small loss, not a neutral outcome.
- This has NO position sizing safety beyond config.TRADE_SIZE_USDT, and NO
  daily loss limit. On testnet that's fine. Do not treat this file as
  production-ready for real funds without adding those.
"""

import sqlite3
from datetime import datetime, timezone
import config
import binance_rest


def init_trades_db():
    conn = sqlite3.connect(config.DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            direction TEXT NOT NULL,
            expected_profit_pct REAL,
            status TEXT NOT NULL,           -- 'success', 'failed_unwound', 'failed_stuck'
            leg1_symbol TEXT, leg1_side TEXT, leg1_result TEXT,
            leg2_symbol TEXT, leg2_side TEXT, leg2_result TEXT,
            leg3_symbol TEXT, leg3_side TEXT, leg3_result TEXT,
            error TEXT
        )
    """)
    conn.commit()
    conn.close()


def _log_trade(direction, expected_profit_pct, status, legs, error=None):
    conn = sqlite3.connect(config.DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO trades (
            timestamp, direction, expected_profit_pct, status,
            leg1_symbol, leg1_side, leg1_result,
            leg2_symbol, leg2_side, leg2_result,
            leg3_symbol, leg3_side, leg3_result,
            error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        direction, expected_profit_pct, status,
        *[str(x) for x in legs[0]], *[str(x) for x in legs[1]], *[str(x) for x in legs[2]],
        str(error) if error else None,
    ))
    conn.commit()
    conn.close()


def execute_forward_loop(expected_profit_pct: float):
    """
    USDT -> BTC -> ETH -> USDT

    Leg 1: BUY BTCUSDT, spend TRADE_SIZE_USDT
    Leg 2: BUY ETHBTC, spend all BTC just acquired
    Leg 3: SELL ETHUSDT, sell all ETH just acquired
    """
    legs = [[config.LEG_1, "BUY", None], [config.LEG_2, "BUY", None], [config.LEG_3, "SELL", None]]
    try:
        r1 = binance_rest.place_market_order(config.LEG_1, "BUY", quote_order_qty=config.TRADE_SIZE_USDT)
        legs[0][2] = r1
        btc_acquired = float(r1["executedQty"])

        r2 = binance_rest.place_market_order(config.LEG_2, "BUY", quote_order_qty=btc_acquired * float(r1["fills"][0]["price"]))
        legs[1][2] = r2
        eth_acquired = float(r2["executedQty"])

        r3 = binance_rest.place_market_order(config.LEG_3, "SELL", quantity=eth_acquired)
        legs[2][2] = r3

        _log_trade("forward", expected_profit_pct, "success", legs)
        print(f"[EXECUTED] Forward loop complete. Leg3 result: {r3.get('status')}")
        return True

    except Exception as e:
        print(f"[EXECUTION FAILED] Forward loop: {e}")
        _attempt_unwind(legs, "forward", expected_profit_pct, e)
        return False


def execute_reverse_loop(expected_profit_pct: float):
    """
    USDT -> ETH -> BTC -> USDT

    Leg 1: BUY ETHUSDT, spend TRADE_SIZE_USDT
    Leg 2: SELL ETHBTC, sell all ETH just acquired
    Leg 3: SELL BTCUSDT, sell all BTC just acquired
    """
    legs = [[config.LEG_3, "BUY", None], [config.LEG_2, "SELL", None], [config.LEG_1, "SELL", None]]
    try:
        r1 = binance_rest.place_market_order(config.LEG_3, "BUY", quote_order_qty=config.TRADE_SIZE_USDT)
        legs[0][2] = r1
        eth_acquired = float(r1["executedQty"])

        r2 = binance_rest.place_market_order(config.LEG_2, "SELL", quantity=eth_acquired)
        legs[1][2] = r2
        btc_acquired = float(r2["executedQty"])

        r3 = binance_rest.place_market_order(config.LEG_1, "SELL", quantity=btc_acquired)
        legs[2][2] = r3

        _log_trade("reverse", expected_profit_pct, "success", legs)
        print(f"[EXECUTED] Reverse loop complete. Leg3 result: {r3.get('status')}")
        return True

    except Exception as e:
        print(f"[EXECUTION FAILED] Reverse loop: {e}")
        _attempt_unwind(legs, "reverse", expected_profit_pct, e)
        return False


def _attempt_unwind(legs, direction, expected_profit_pct, error):
    """
    Best-effort: if we got partway through the loop, sell back whatever we're
    now holding so we're not left with unintended market exposure. This is a
    guaranteed small loss (fee + slippage), not a neutral outcome.
    """
    filled_legs = [leg for leg in legs if leg[2] is not None]

    if not filled_legs:
        # nothing executed at all, nothing to unwind
        _log_trade(direction, expected_profit_pct, "failed_stuck", legs, error)
        return

    print(f"[UNWIND] {len(filled_legs)} leg(s) filled before failure - attempting to flatten position...")
    try:
        last_filled = filled_legs[-1]
        symbol, side, result = last_filled
        opposite_side = "SELL" if side == "BUY" else "BUY"
        qty = float(result["executedQty"])
        binance_rest.place_market_order(symbol, opposite_side, quantity=qty)
        _log_trade(direction, expected_profit_pct, "failed_unwound", legs, error)
        print("[UNWIND] Position flattened.")
    except Exception as unwind_error:
        # this is the bad case - manual intervention needed on the actual account
        _log_trade(direction, expected_profit_pct, "failed_stuck", legs,
                    f"{error} | UNWIND ALSO FAILED: {unwind_error}")
        print(f"[UNWIND FAILED] Manual check of account required: {unwind_error}")