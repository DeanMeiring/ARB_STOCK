"""
Executes a triangular loop as 3 sequential MARKET orders against Binance
Testnet (or wherever config.BINANCE_BASE_URL points - default is Testnet,
and it should stay that way until you've reviewed this code and governor.py
very carefully).

IMPORTANT - read before enabling config.EXECUTE_TRADES:

- Every attempt is gated by governor.check() first - kill switch, cumulative/
  daily loss limits, trade-count cap, min-gap-between-trades. See governor.py.
  No order is placed if that check fails.
- Orders are sent ONE AT A TIME, not atomically. Between the theoretical
  calculation and each order actually filling, prices move. The real
  profit/loss will differ from what the detector calculated - sometimes
  a lot.
- If leg 2 or 3 fails after leg 1 succeeded, this code attempts to unwind
  by selling back what it just bought, AT MARKET, immediately. That unwind
  itself costs a taker fee and slippage - a failed loop is a guaranteed
  small loss, not a neutral outcome.
- Realized P&L is computed from Binance's actual fill data (cummulativeQuoteQty),
  not the pre-trade theoretical estimate. It's only ever cleanly USDT-
  denominated when the position unwinds back through leg1's symbol (always a
  *USDT pair) - a leg-3 failure unwinds through ETHBTC instead, leaving BTC
  exposure that isn't cleanly convertible to a USDT P&L number here. That
  case logs profit_usdt=NULL, which governor.py treats as an automatic
  kill-switch trip (an unknown P&L means the account may be sitting in an
  untracked position - exactly the scenario that needs a human, not a
  best-effort guess).
"""

import config
import binance_rest
import governor
import logger


def _quote_spent(order_result) -> float:
    """USDT (or other quote asset) actually moved on this leg, per Binance's fill data."""
    return float(order_result["cummulativeQuoteQty"])


def _log_and_govern(direction, expected_profit_pct, status, legs, start_usdt=None, end_usdt=None,
                     profit_usdt=None, error=None):
    logger.log_trade(direction, expected_profit_pct, status, legs,
                      start_usdt=start_usdt, end_usdt=end_usdt, profit_usdt=profit_usdt, error=str(error) if error else None)
    governor.record_outcome(profit_usdt)


def execute_forward_loop(expected_profit_pct: float):
    """
    USDT -> BTC -> ETH -> USDT

    Leg 1: BUY BTCUSDT, spend TRADE_SIZE_USDT
    Leg 2: BUY ETHBTC, spend all BTC just acquired
    Leg 3: SELL ETHUSDT, sell all ETH just acquired
    """
    allowed, reason = governor.check()
    if not allowed:
        print(f"[GOVERNOR BLOCKED] Forward loop: {reason}")
        return False

    legs = [[config.LEG_1, "BUY", None], [config.LEG_2, "BUY", None], [config.LEG_3, "SELL", None]]
    try:
        r1 = binance_rest.place_market_order(config.LEG_1, "BUY", quote_order_qty=config.TRADE_SIZE_USDT)
        legs[0][2] = r1
        start_usdt = _quote_spent(r1)
        btc_acquired = float(r1["executedQty"])

        r2 = binance_rest.place_market_order(config.LEG_2, "BUY", quote_order_qty=btc_acquired)
        legs[1][2] = r2
        eth_acquired = float(r2["executedQty"])

        r3 = binance_rest.place_market_order(config.LEG_3, "SELL", quantity=eth_acquired)
        legs[2][2] = r3
        end_usdt = _quote_spent(r3)

        profit_usdt = end_usdt - start_usdt
        _log_and_govern("forward", expected_profit_pct, "success", legs,
                         start_usdt=start_usdt, end_usdt=end_usdt, profit_usdt=profit_usdt)
        print(f"[EXECUTED] Forward loop complete. profit: ${profit_usdt:.4f}")
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
    allowed, reason = governor.check()
    if not allowed:
        print(f"[GOVERNOR BLOCKED] Reverse loop: {reason}")
        return False

    legs = [[config.LEG_3, "BUY", None], [config.LEG_2, "SELL", None], [config.LEG_1, "SELL", None]]
    try:
        r1 = binance_rest.place_market_order(config.LEG_3, "BUY", quote_order_qty=config.TRADE_SIZE_USDT)
        legs[0][2] = r1
        start_usdt = _quote_spent(r1)
        eth_acquired = float(r1["executedQty"])

        r2 = binance_rest.place_market_order(config.LEG_2, "SELL", quantity=eth_acquired)
        legs[1][2] = r2
        btc_acquired = float(r2["executedQty"])

        r3 = binance_rest.place_market_order(config.LEG_1, "SELL", quantity=btc_acquired)
        legs[2][2] = r3
        end_usdt = _quote_spent(r3)

        profit_usdt = end_usdt - start_usdt
        _log_and_govern("reverse", expected_profit_pct, "success", legs,
                         start_usdt=start_usdt, end_usdt=end_usdt, profit_usdt=profit_usdt)
        print(f"[EXECUTED] Reverse loop complete. profit: ${profit_usdt:.4f}")
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

    P&L only comes out cleanly in USDT when exactly one leg filled - that leg
    is always a *USDT pair (BTCUSDT or ETHUSDT), so unwinding it returns
    straight to USDT. Two legs filled means the second leg was ETHBTC, whose
    unwind returns BTC, not USDT - see module docstring for why that logs as
    unknown P&L rather than a guess.
    """
    filled_legs = [leg for leg in legs if leg[2] is not None]

    if not filled_legs:
        # leg1 itself failed - nothing was ever placed, zero exposure, zero loss.
        _log_and_govern(direction, expected_profit_pct, "failed_stuck", legs, profit_usdt=0.0, error=error)
        return

    print(f"[UNWIND] {len(filled_legs)} leg(s) filled before failure - attempting to flatten position...")
    try:
        last_filled = filled_legs[-1]
        symbol, side, result = last_filled
        opposite_side = "SELL" if side == "BUY" else "BUY"
        qty = float(result["executedQty"])
        unwind_result = binance_rest.place_market_order(symbol, opposite_side, quantity=qty)

        if len(filled_legs) == 1:
            start_usdt = _quote_spent(result)
            end_usdt = _quote_spent(unwind_result)
            profit_usdt = end_usdt - start_usdt
        else:
            # unwound through ETHBTC, not back to USDT - see docstring
            profit_usdt = None

        _log_and_govern(direction, expected_profit_pct, "failed_unwound", legs, profit_usdt=profit_usdt, error=error)
        print(f"[UNWIND] Position flattened. profit: {profit_usdt if profit_usdt is not None else 'unknown'}")
    except Exception as unwind_error:
        # this is the bad case - manual intervention needed on the actual account
        _log_and_govern(direction, expected_profit_pct, "failed_stuck", legs, profit_usdt=None,
                         error=f"{error} | UNWIND ALSO FAILED: {unwind_error}")
        print(f"[UNWIND FAILED] Manual check of account required: {unwind_error}")
