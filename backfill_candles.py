"""
One-off historical backfill for market_candles - run once (or whenever you
want to extend the window further back), not on a schedule.

Binance's public klines endpoint supports genuine historical range queries
(startTime/endTime, paginated), so BTCUSDT/ETHBTC/ETHUSDT get real depth -
BACKFILL_DAYS below controls how far back.

Crypto.com's public candlestick endpoint does NOT support date-range
pagination - it only returns its most recent ~50-300 candles regardless of
what you ask for. So CRYPTOCOM_BTC_USDT can't be backfilled the same way;
its history can only grow organically from the live collection already
running in main.py. This script still pulls whatever recent window
Crypto.com's endpoint gives, since a little is better than nothing, but
don't expect it to match Binance's depth.

Run:
    python backfill_candles.py
"""

import time
from datetime import datetime, timedelta, timezone
import requests
import config
import logger

# 90 days of 1-minute candles per symbol (~130k rows) - deep enough to give
# the price-trend model several distinct market regimes to learn from
# instead of whatever few weeks happened to accumulate organically. Bigger
# than this starts trading meaningfully more Postgres storage for training
# data the model may not even weight heavily (recent regime usually matters
# most) - 90 was picked as a middle ground, not a hard limit.
BACKFILL_DAYS = 90
# Triangular symbols (ETHBTC has no price-trend model, only used for the arb
# loop) plus every coin the price-trend model trains on - deduped, in case
# of overlap (BTCUSDT/ETHUSDT are in both).
BINANCE_SYMBOLS = list(dict.fromkeys(config.SYMBOLS + config.PREDICT_SYMBOLS))
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


def fetch_binance_klines(symbol: str, start_ms: int, end_ms: int) -> list:
    """Paginates 1000-candle pages until end_ms is reached. Returns
    (symbol, candle_start, open, high, low, close, tick_count, volume) tuples -
    volume is index 5 of Binance's kline array, always present regardless of
    symbol, so this captures it for every backfill, not just PREDICT_SYMBOLS
    (harmless/unused for symbols with no price-trend model, e.g. ETHBTC)."""
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        resp = requests.get(BINANCE_KLINES_URL, params={
            "symbol": symbol, "interval": "1m", "startTime": cursor,
            "endTime": end_ms, "limit": 1000,
        }, timeout=15)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for k in batch:
            open_time_ms, o, h, l, c, v = k[0], k[1], k[2], k[3], k[4], k[5]
            candle_start = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
            rows.append((symbol, candle_start, float(o), float(h), float(l), float(c), None, float(v)))
        cursor = batch[-1][0] + 60_000  # advance past the last candle's open time
        time.sleep(0.2)  # stay well clear of rate limits
    return rows


def backfill_binance():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=BACKFILL_DAYS)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    for symbol in BINANCE_SYMBOLS:
        print(f"Fetching {BACKFILL_DAYS}d of 1m candles for {symbol}...")
        rows = fetch_binance_klines(symbol, start_ms, end_ms)
        print(f"  {len(rows)} candles fetched, writing to Postgres...")
        logger.bulk_log_candles(rows)
        print(f"  {symbol} done.")


def backfill_cryptocom():
    print("Fetching Crypto.com's recent candle window (no deep history available)...")
    try:
        resp = requests.get(f"{config.CRYPTOCOM_REST_BASE}/get-candlestick", params={
            "instrument_name": config.CRYPTOCOM_SYMBOL, "timeframe": "1m",
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            print(f"  Crypto.com backfill failed: {data}")
            return
        candles = data.get("result", {}).get("data", [])
        rows = []
        for c in candles:
            candle_start = datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc)
            rows.append((
                "CRYPTOCOM_BTC_USDT", candle_start,
                float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]), None,
                float(c["v"]) if c.get("v") is not None else None,
            ))
        logger.bulk_log_candles(rows)
        print(f"  {len(rows)} Crypto.com candles written.")
    except Exception as e:
        print(f"  Crypto.com backfill error (Binance backfill above is unaffected): {e}")


def main():
    backfill_binance()
    backfill_cryptocom()
    print("Backfill complete.")


if __name__ == "__main__":
    main()
