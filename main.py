"""
ARB_STOCK - Triangular Arbitrage Monitor (Binance, USDT/BTC/ETH loop)

Detection, logging & Telegram alerts only - this does NOT place real trades.
It watches live prices, calculates the theoretical return of looping through
USDT -> BTC -> ETH -> USDT (and the reverse direction), logs any opportunity
that clears the fee-adjusted profit threshold to Postgres, and messages
anyone who has /login'd via the Telegram bot so it can be executed manually.

Run:
    python main.py
"""

import asyncio
import config
import logger
from binance_client import BinanceBookTickerStream
from arb_calculator import check_both_directions
from telegram_bot import TelegramNotifier

if config.EXECUTE_TRADES:
    import executor

# simple in-memory counters for a live status line
stats = {"ticks": 0, "opportunities": 0}

notifier = TelegramNotifier()

# best sub-threshold result seen since the last flush - not persisted per-tick
# (hundreds/sec), just sampled periodically for the /report "closest miss" stat
best_miss = {"direction": None, "profit_pct": None, "profit_usdt": None}


async def on_price_update(latest: dict):
    stats["ticks"] += 1

    btcusdt = latest.get(config.LEG_1.upper())
    ethbtc = latest.get(config.LEG_2.upper())
    ethusdt = latest.get(config.LEG_3.upper())

    if not (btcusdt and ethbtc and ethusdt):
        return  # still waiting on one of the three streams

    fwd, rev = check_both_directions(btcusdt, ethbtc, ethusdt)

    # debug: show how close the best direction is getting to the threshold,
    # even when it doesn't clear it - useful to confirm the math is sane
    if stats["ticks"] % 500 == 0:
        best = max(fwd, rev, key=lambda r: r.profit_pct)
        print(f"    [debug] best loop this tick: {best.direction} at {best.profit_pct*100:.4f}% "
              f"(threshold: {config.MIN_PROFIT_THRESHOLD*100:.4f}%)")

    for result in (fwd, rev):
        if result.is_opportunity:
            stats["opportunities"] += 1
            logger.log_opportunity(result, btcusdt, ethbtc, ethusdt)
            await notifier.notify_opportunity(result)
            print(
                f"[OPPORTUNITY] {result.direction} | "
                f"profit: {result.profit_pct*100:.4f}% "
                f"(${result.profit_usdt:.2f} on ${result.start_usdt:.0f}) "
                f"| BTCUSDT {btcusdt.bid}/{btcusdt.ask} "
                f"ETHBTC {ethbtc.bid}/{ethbtc.ask} "
                f"ETHUSDT {ethusdt.bid}/{ethusdt.ask}"
            )

            if config.EXECUTE_TRADES:
                if "forward" in result.direction:
                    executor.execute_forward_loop(result.profit_pct)
                else:
                    executor.execute_reverse_loop(result.profit_pct)
        elif best_miss["profit_pct"] is None or result.profit_pct > best_miss["profit_pct"]:
            best_miss["direction"] = result.direction
            best_miss["profit_pct"] = result.profit_pct
            best_miss["profit_usdt"] = result.profit_usdt

    # lightweight heartbeat every 200 ticks so you know it's alive
    if stats["ticks"] % 200 == 0:
        print(f"... {stats['ticks']} ticks processed, {stats['opportunities']} opportunities logged so far")


async def flush_near_miss_periodically():
    """Every 30s, persist the best sub-threshold result seen and reset it."""
    while True:
        await asyncio.sleep(30)
        if best_miss["profit_pct"] is not None:
            logger.log_near_miss(best_miss["direction"], best_miss["profit_pct"], best_miss["profit_usdt"])
            best_miss["direction"] = None
            best_miss["profit_pct"] = None
            best_miss["profit_usdt"] = None


async def main():
    logger.init_db()
    if config.EXECUTE_TRADES:
        executor.init_trades_db()
        print("*** EXECUTE_TRADES is ON - real orders will be placed against "
              f"{config.BINANCE_BASE_URL} ***")
    stream = BinanceBookTickerStream(config.SYMBOLS)
    await asyncio.gather(
        stream.run(on_price_update),
        notifier.poll_forever(),
        flush_near_miss_periodically(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\nStopped. Total: {stats['ticks']} ticks, {stats['opportunities']} opportunities logged.")