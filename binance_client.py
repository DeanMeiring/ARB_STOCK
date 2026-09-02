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
                        )

                        # only fire the callback once we have all three symbols
                        if len(self.latest) == len(self.symbols):
                            await on_update(self.latest)

            except (websockets.exceptions.ConnectionClosed, OSError) as e:
                print(f"WebSocket dropped ({e}), reconnecting in 3s...")
                await asyncio.sleep(3)
