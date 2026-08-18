"""
ARB_STOCK - Triangular Arbitrage Monitor (Binance, USDT/BTC/ETH loop)

Detection & logging only - this does NOT place real trades. It watches live
prices, calculates the theoretical return of looping through
USDT -> BTC -> ETH -> USDT (and the reverse direction), and logs any
opportunity that clears the fee-adjusted profit threshold.

Run:
    python main.py
"""

import asyncio
import config
import logger
from binance_client import BinanceBookTickerStream
from arb_calculator import check_both_directions

# simple in-memory counters for a live status line
stats = {"ticks": 0, "opportunities": 0}


async def on_price_update(latest: dict):
    stats["ticks"] += 1

    btcusdt = latest.get(config.LEG_1.upper())
    ethbtc = latest.get(config.LEG_2.upper())
    ethusdt = latest.get(config.LEG_3.upper())

    if not (btcusdt and ethbtc and ethusdt):
        return  # still waiting on one of the three streams

    fwd, rev = check_both_directions(btcusdt, ethbtc, ethusdt)

    for result in (fwd, rev):
        if result.is_opportunity:
            stats["opportunities"] += 1
            logger.log_opportunity(result, btcusdt, ethbtc, ethusdt)
            print(
                f"[OPPORTUNITY] {result.direction} | "
                f"profit: {result.profit_pct*100:.4f}% "
                f"(${result.profit_usdt:.2f} on ${result.start_usdt:.0f}) "
                f"| BTCUSDT {btcusdt.bid}/{btcusdt.ask} "
                f"ETHBTC {ethbtc.bid}/{ethbtc.ask} "
                f"ETHUSDT {ethusdt.bid}/{ethusdt.ask}"
            )

    # lightweight heartbeat every 200 ticks so you know it's alive
    if stats["ticks"] % 200 == 0:
        print(f"... {stats['ticks']} ticks processed, {stats['opportunities']} opportunities logged so far")


async def main():
    logger.init_db()
    stream = BinanceBookTickerStream(config.SYMBOLS)
    await stream.run(on_price_update)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\nStopped. Total: {stats['ticks']} ticks, {stats['opportunities']} opportunities logged.")