"""
ARB_STOCK - Arbitrage Monitor (Binance triangular + Binance/Crypto.com cross-exchange)

Detection, logging & Telegram alerts only - this does NOT place real trades.
Two independent detectors run side by side:
  1. Triangular: loops USDT -> BTC -> ETH -> USDT (and the reverse) on
     Binance alone, using its live WebSocket book-ticker feed.
  2. Cross-exchange: compares Binance's BTCUSDT against Crypto.com's
     BTC_USDT, polled over REST (no WS schema for Crypto.com was
     verifiable from the dev environment, so REST was the safer choice).
Both log any opportunity that clears the fee-adjusted profit threshold to
Postgres, and message anyone who has /login'd via the Telegram bot so it
can be executed manually.

Run:
    python main.py
"""

import asyncio
from datetime import datetime, timezone
import config
import logger
from binance_client import BinanceBookTickerStream
from arb_calculator import check_both_directions, check_cross_exchange
import cryptocom_client
from telegram_bot import TelegramNotifier

if config.EXECUTE_TRADES:
    import executor

# simple in-memory counters for a live status line
stats = {"ticks": 0, "opportunities": 0, "cross_exchange_opportunities": 0}

notifier = TelegramNotifier()
stream = BinanceBookTickerStream(config.SYMBOLS)

# best sub-threshold result seen since the last flush, per source - not
# persisted per-tick (hundreds/sec for the triangular side), just sampled
# periodically for the /report "closest miss" stat
best_miss = {
    "triangular": {"direction": None, "profit_pct": None, "profit_usdt": None},
    "cross_exchange": {"direction": None, "profit_pct": None, "profit_usdt": None},
}

# updated on every Binance tick - the watchdog task uses this to detect a
# stuck/dropped feed. Cross-exchange polling doesn't feed this - it's a
# lower-criticality, separate subsystem and a Crypto.com hiccup shouldn't
# trigger the same "feed is stuck" alert as the primary Binance feed dying.
last_tick_at = {"time": None}

# 1-minute OHLC candles (built from mid-price) in progress, per symbol -
# this is the actual price history for training a trend model later.
# Flushed to Postgres every 60s by flush_candles_periodically.
candles_in_progress = {}


def _track_near_miss(source, result):
    slot = best_miss[source]
    if slot["profit_pct"] is None or result.profit_pct > slot["profit_pct"]:
        slot["direction"] = result.direction
        slot["profit_pct"] = result.profit_pct
        slot["profit_usdt"] = result.profit_usdt


def _update_candle(symbol, bid, ask):
    price = (bid + ask) / 2
    c = candles_in_progress.get(symbol)
    if c is None:
        candles_in_progress[symbol] = {
            "open": price, "high": price, "low": price, "close": price,
            "count": 1, "start": datetime.now(timezone.utc),
        }
    else:
        c["high"] = max(c["high"], price)
        c["low"] = min(c["low"], price)
        c["close"] = price
        c["count"] += 1


async def on_price_update(latest: dict):
    stats["ticks"] += 1
    last_tick_at["time"] = datetime.now(timezone.utc)

    btcusdt = latest.get(config.LEG_1.upper())
    ethbtc = latest.get(config.LEG_2.upper())
    ethusdt = latest.get(config.LEG_3.upper())

    if not (btcusdt and ethbtc and ethusdt):
        return  # still waiting on one of the three streams

    _update_candle("BTCUSDT", btcusdt.bid, btcusdt.ask)
    _update_candle("ETHBTC", ethbtc.bid, ethbtc.ask)
    _update_candle("ETHUSDT", ethusdt.bid, ethusdt.ask)

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
            logger.log_opportunity(result, "triangular", btcusdt=btcusdt, ethbtc=ethbtc, ethusdt=ethusdt)
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
        else:
            _track_near_miss("triangular", result)

    # lightweight heartbeat every 200 ticks so you know it's alive
    if stats["ticks"] % 200 == 0:
        print(f"... {stats['ticks']} ticks processed, {stats['opportunities']} opportunities logged so far")


async def cross_exchange_watch():
    """
    Every CRYPTOCOM_POLL_SECONDS, compares Binance's live BTCUSDT (read from
    the already-running stream, not re-fetched) against a fresh Crypto.com
    poll. Isolated try/except so a Crypto.com API hiccup or schema mismatch
    never takes down the primary triangular detector.
    """
    while True:
        await asyncio.sleep(config.CRYPTOCOM_POLL_SECONDS)
        try:
            binance_ticker = stream.latest.get(config.LEG_1.upper())
            if not binance_ticker:
                continue  # Binance side not warmed up yet

            cryptocom_ticker = await asyncio.to_thread(cryptocom_client.get_ticker, config.CRYPTOCOM_SYMBOL)
            _update_candle("CRYPTOCOM_BTC_USDT", cryptocom_ticker.bid, cryptocom_ticker.ask)

            for result in check_cross_exchange(binance_ticker, cryptocom_ticker):
                if result.is_opportunity:
                    stats["cross_exchange_opportunities"] += 1
                    logger.log_opportunity(result, "cross_exchange", btcusdt=binance_ticker, cryptocom=cryptocom_ticker)
                    await notifier.notify_opportunity(result)
                    print(
                        f"[CROSS-EXCHANGE OPPORTUNITY] {result.direction} | "
                        f"profit: {result.profit_pct*100:.4f}% (${result.profit_usdt:.2f} on ${result.start_usdt:.0f}) "
                        f"| Binance {binance_ticker.bid}/{binance_ticker.ask} "
                        f"| Crypto.com {cryptocom_ticker.bid}/{cryptocom_ticker.ask}"
                    )
                else:
                    _track_near_miss("cross_exchange", result)
        except Exception as e:
            print(f"[cross-exchange] error: {e}")


async def flush_near_miss_periodically():
    """Every 30s, persist the best sub-threshold result seen (per source) and reset it."""
    while True:
        await asyncio.sleep(30)
        for source, slot in best_miss.items():
            if slot["profit_pct"] is not None:
                logger.log_near_miss(slot["direction"], slot["profit_pct"], slot["profit_usdt"], source=source)
                slot["direction"] = None
                slot["profit_pct"] = None
                slot["profit_usdt"] = None


async def flush_candles_periodically():
    """Every 60s, close out each symbol's in-progress candle and persist it."""
    while True:
        await asyncio.sleep(60)
        for symbol, c in list(candles_in_progress.items()):
            logger.log_candle(symbol, c["start"], c["open"], c["high"], c["low"], c["close"], c["count"])
            del candles_in_progress[symbol]


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
                f"{stats['opportunities']} triangular + {stats['cross_exchange_opportunities']} "
                f"cross-exchange opportunities since last heartbeat."
            )
            ticks_at_last_heartbeat = stats["ticks"]
            last_heartbeat_at = now


async def main():
    logger.init_db()
    if config.EXECUTE_TRADES:
        executor.init_trades_db()
        print("*** EXECUTE_TRADES is ON - real orders will be placed against "
              f"{config.BINANCE_BASE_URL} ***")
    await asyncio.gather(
        stream.run(on_price_update),
        cross_exchange_watch(),
        notifier.poll_forever(),
        flush_near_miss_periodically(),
        flush_candles_periodically(),
        watchdog(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\nStopped. Total: {stats['ticks']} ticks, {stats['opportunities']} opportunities logged.")
