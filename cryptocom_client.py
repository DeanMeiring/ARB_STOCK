"""
Crypto.com Exchange public REST ticker client.

No authentication needed - this is public market data. Polled on a timer
(see main.py's cross_exchange_watch), not push-based like Binance's WS,
since the exact WS message schema wasn't verifiable from this environment
and a REST poll is simpler to get right and to reason about when it fails.

Docs: https://exchange-docs.crypto.com/exchange/v1/rest-ws/index.html#public-get-ticker
"""

import requests
import config
from arb_calculator import BookTicker


class CryptoComError(Exception):
    pass


def get_ticker(symbol: str) -> BookTicker:
    resp = requests.get(
        f"{config.CRYPTOCOM_REST_BASE}/get-ticker",
        params={"instrument_name": symbol},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != 0:
        raise CryptoComError(f"get_ticker({symbol}) failed: {data}")

    rows = data.get("result", {}).get("data", [])
    if not rows:
        raise CryptoComError(f"get_ticker({symbol}) returned no data: {data}")

    row = rows[0]
    return BookTicker(symbol=symbol, bid=float(row["b"]), ask=float(row["k"]))
