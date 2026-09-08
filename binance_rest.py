"""
Minimal signed REST client for Binance (Testnet by default via config.BINANCE_BASE_URL).

Only implements what the executor needs: account balance and market orders.
Uses HMAC-SHA256 request signing per Binance's spot API auth spec.

Docs: https://binance-docs.github.io/apidocs/spot/en/#signed-trade-and-user_data-endpoints-security

If config.BINANCE_PROXY_URL is set, these signed calls route through it
instead of going out directly - Binance sees the proxy's fixed IP, which is
what you whitelist on a trade-permission key (Binance requires those to be
IP-restricted; Railway's own outbound IP isn't static without a paid add-on).
Leave unset for Testnet/read-only use, where IP restriction doesn't apply.
"""

import hashlib
import hmac
import time
import urllib.parse
import requests
import config


class BinanceRestError(Exception):
    pass


def _sign(params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    signature = hmac.new(
        config.BINANCE_API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    params["signature"] = signature
    return params


def _headers():
    return {"X-MBX-APIKEY": config.BINANCE_API_KEY}


def _proxies():
    if not config.BINANCE_PROXY_URL:
        return None
    return {"http": config.BINANCE_PROXY_URL, "https": config.BINANCE_PROXY_URL}


def _check_keys():
    if not config.BINANCE_API_KEY or not config.BINANCE_API_SECRET:
        raise BinanceRestError(
            "Missing API credentials. Set BINANCE_API_KEY and BINANCE_API_SECRET "
            "as environment variables (get free testnet keys at https://testnet.binance.vision/)."
        )


def get_account_balances() -> dict:
    """Returns {asset: free_balance} for all non-zero balances."""
    _check_keys()
    params = {"timestamp": int(time.time() * 1000)}
    params = _sign(params)
    resp = requests.get(
        f"{config.BINANCE_BASE_URL}/api/v3/account",
        params=params,
        headers=_headers(),
        proxies=_proxies(),
        timeout=10,
    )
    if resp.status_code != 200:
        raise BinanceRestError(f"get_account_balances failed: {resp.status_code} {resp.text}")

    data = resp.json()
    return {
        b["asset"]: float(b["free"])
        for b in data.get("balances", [])
        if float(b["free"]) > 0
    }


def place_market_order(symbol: str, side: str, quote_order_qty: float = None, quantity: float = None) -> dict:
    """
    Places a MARKET order.

    side: "BUY" or "SELL"
    quote_order_qty: spend this much of the QUOTE asset (e.g. spend X USDT buying BTC) - use for BUY legs
    quantity: sell this much of the BASE asset (e.g. sell X ETH for BTC) - use for SELL legs

    Exactly one of quote_order_qty / quantity should be set - matches how you'd
    naturally think about each leg of the loop (spend a known amount vs sell a known amount).
    """
    _check_keys()
    if (quote_order_qty is None) == (quantity is None):
        raise ValueError("Provide exactly one of quote_order_qty or quantity")

    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "timestamp": int(time.time() * 1000),
    }
    if quote_order_qty is not None:
        params["quoteOrderQty"] = round(quote_order_qty, 8)
    else:
        params["quantity"] = round(quantity, 8)

    params = _sign(params)
    resp = requests.post(
        f"{config.BINANCE_BASE_URL}/api/v3/order",
        params=params,
        headers=_headers(),
        proxies=_proxies(),
        timeout=10,
    )
    if resp.status_code != 200:
        raise BinanceRestError(f"place_market_order({symbol}, {side}) failed: {resp.status_code} {resp.text}")

    return resp.json()