"""
Binance WebSocket client.

Uses the combined stream endpoint to subscribe to bookTicker updates for all
three symbols at once. bookTicker pushes the best bid/ask in real time -
this is a lot faster and cheaper than polling REST endpoints, and it's the
same style of data feed real trading systems use.

Docs: https://binance-docs.github.io/apidocs/spot/en/#individual-symbol-book-ticker-streams
"""

import asyncio
import json
import websockets
import config
from arb_calculator import BookTicker


class BinanceBookTickerStream:
    def __init__(self, symbols):
        self.symbols = [s.lower() for s in symbols]
        streams = "/".join(f"{s}@bookTicker" for s in self.symbols)
        self.url = f"{config.BINANCE_WS_BASE}?streams={streams}"
        # latest known state for each symbol, kept in memory
        self.latest = {}

    async def run(self, on_update):
        """
        Connects and reconnects on drop. Calls on_update(latest_dict) every
        time we have a fresh tick for any symbol.
        """
        while True:
            try:
                print(f"Connecting to Binance WS: {self.url}")
                async with websockets.connect(self.url, ping_interval=20, open_timeout=15) as ws:
                    print(f"Connected to Binance WS: {self.symbols}")
                    async for message in ws:
                        data = json.loads(message)
                        payload = data.get("data", {})
                        symbol = payload.get("s")
                        if not symbol:
                            continue

                        self.latest[symbol] = BookTicker(
                            symbol=symbol,
                            bid=float(payload["b"]),
                            ask=float(payload["a"]),
                            # B/A: resting quantity at the best bid/ask - already
                            # in every bookTicker message, just unused until now.
                            bid_qty=float(payload["B"]),
                            ask_qty=float(payload["A"]),
                        )

                        # only fire the callback once we have all three symbols
                        if len(self.latest) == len(self.symbols):
                            await on_update(self.latest)

            except (websockets.exceptions.WebSocketException, OSError) as e:
                # WebSocketException covers handshake-level rejections (e.g. an
                # HTTP 451/403 InvalidStatus) as well as drops mid-connection -
                # letting any of those crash the process instead of retrying is
                # how a single rejected handshake turns into a Railway crash loop.
                print(f"WebSocket dropped ({e}), reconnecting in 3s...")
                await asyncio.sleep(3)


class BinanceKlineVolumeStream:
    """
    Tracks each symbol's most recently CLOSED 1-minute traded volume, from
    Binance's kline_1m stream - a second, independent WS connection purely
    for that one field, since bookTicker (used above for price/the arb
    detector) carries no trade volume at all.

    This is an approximation, not an exact match to main.py's own candle
    boundaries: BinanceBookTickerStream's in-memory candles are built on
    this process's own wall-clock minute boundary (whenever
    flush_candles_periodically's 60s loop happens to tick), while Binance's
    kline stream closes on its own UTC minute boundary - the two can drift
    by up to the flush loop's phase offset (well under 60s in practice).
    Good enough for a "how busy was this roughly one-minute window" feature,
    not meant to be exact.
    """
    def __init__(self, symbols):
        self.symbols = [s.lower() for s in symbols]
        streams = "/".join(f"{s}@kline_1m" for s in self.symbols)
        self.url = f"{config.BINANCE_WS_BASE}?streams={streams}"
        self.latest_volume = {}  # symbol -> most recently closed candle's volume

    async def run(self):
        while True:
            try:
                print(f"Connecting to Binance kline WS: {self.url}")
                async with websockets.connect(self.url, ping_interval=20, open_timeout=15) as ws:
                    print(f"Connected to Binance kline WS: {self.symbols}")
                    async for message in ws:
                        data = json.loads(message)
                        k = data.get("data", {}).get("k")
                        if not k or not k.get("x"):
                            continue  # only closed candles - a still-forming one's volume is partial
                        symbol = k.get("s")
                        if symbol:
                            self.latest_volume[symbol] = float(k["v"])

            except (websockets.exceptions.WebSocketException, OSError) as e:
                print(f"Kline WebSocket dropped ({e}), reconnecting in 3s...")
                await asyncio.sleep(3)
