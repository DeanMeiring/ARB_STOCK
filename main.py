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
from datetime import datetime, timezone
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

# updated on every tick - the watchdog task uses this to detect a stuck/dropped feed
last_tick_at = {"time": None}


async def on_price_update(latest: dict):
    stats["ticks"] += 1
    last_tick_at["time"] = datetime.now(timezone.utc)

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


async def watchdog():
    """
    Two independent checks on a 60s tick:
    - Stale feed: alert once if no price tick has arrived in
      STALE_TICK_ALERT_SECONDS, and once more when it recovers. Guards
      against a silently stuck/disconnected feed going unnoticed for days.
    - Heartbeat: an unconditional "still running" message every
      HEARTBEAT_INTERVAL_SECONDS, so a full process hang (not just a
      dropped WS) is also visible - if this message stops, the whole bot
      has stopped, not just the price feed.
    """
    stale_alerted = False
    last_heartbeat_at = datetime.now(timezone.utc)
    ticks_at_last_heartbeat = 0

    while True:
        await asyncio.sleep(60)
        now = datetime.now(timezone.utc)

        last_tick = last_tick_at["time"]
        seconds_since_tick = (now - last_tick).total_seconds() if last_tick else None
        is_stale = seconds_since_tick is None or seconds_since_tick > config.STALE_TICK_ALERT_SECONDS

        if is_stale and not stale_alerted:
            stale_alerted = True
            await notifier.send_alert(
                f"⚠️ No price ticks in over {config.STALE_TICK_ALERT_SECONDS // 60} min - "
                "the feed may be stuck or disconnected. Check Railway logs."
            )
        elif not is_stale and stale_alerted:
            stale_alerted = False
            await notifier.send_alert("✅ Back up - price ticks flowing again.")

        if (now - last_heartbeat_at).total_seconds() >= config.HEARTBEAT_INTERVAL_SECONDS:
            ticks_since = stats["ticks"] - ticks_at_last_heartbeat
            await notifier.send_alert(
                f"✅ Still running. {ticks_since} ticks, "
                f"{stats['opportunities']} opportunities since last heartbeat."
            )
            ticks_at_last_heartbeat = stats["ticks"]
            last_heartbeat_at = now


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
        watchdog(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\nStopped. Total: {stats['ticks']} ticks, {stats['opportunities']} opportunities logged.")