"""
Triangular arbitrage math.

Given best bid/ask for three pairs (BTCUSDT, ETHBTC, ETHUSDT), calculate the
theoretical return of running the loop in both directions, after fees.

We use bid/ask (not mid-price) because that's what you could ACTUALLY trade
at - using mid-price is a classic mistake that makes opportunities look real
when they're actually just spread.
"""

from dataclasses import dataclass
import config


@dataclass
class BookTicker:
    symbol: str
    bid: float  # best bid price (what someone will buy from you at)
    ask: float  # best ask price (what someone will sell to you at)


@dataclass
class ArbResult:
    direction: str
    start_usdt: float
    end_usdt: float
    profit_pct: float
    profit_usdt: float
    is_opportunity: bool


def forward_loop(btcusdt: BookTicker, ethbtc: BookTicker, ethusdt: BookTicker,
                  start_usdt: float, fee: float) -> ArbResult:
    """
    USDT -> BTC -> ETH -> USDT

    Step 1: buy BTC with USDT at the ASK price (you're a taker buying)
    Step 2: buy ETH with BTC at the ASK price
    Step 3: sell ETH for USDT at the BID price (you're a taker selling)
    """
    btc_bought = (start_usdt / btcusdt.ask) * (1 - fee)
    eth_bought = (btc_bought / ethbtc.ask) * (1 - fee)
    usdt_final = (eth_bought * ethusdt.bid) * (1 - fee)

    profit_usdt = usdt_final - start_usdt
    profit_pct = profit_usdt / start_usdt

    return ArbResult(
        direction="forward (USDT->BTC->ETH->USDT)",
        start_usdt=start_usdt,
        end_usdt=usdt_final,
        profit_pct=profit_pct,
        profit_usdt=profit_usdt,
        is_opportunity=profit_pct > config.MIN_PROFIT_THRESHOLD,
    )


def reverse_loop(btcusdt: BookTicker, ethbtc: BookTicker, ethusdt: BookTicker,
                  start_usdt: float, fee: float) -> ArbResult:
    """
    USDT -> ETH -> BTC -> USDT (the other direction around the same triangle)

    Step 1: buy ETH with USDT at the ASK price
    Step 2: sell ETH for BTC at the BID price (ETHBTC bid = how much BTC you get per ETH)
    Step 3: sell BTC for USDT at the BID price
    """
    eth_bought = (start_usdt / ethusdt.ask) * (1 - fee)
    btc_bought = (eth_bought * ethbtc.bid) * (1 - fee)
    usdt_final = (btc_bought * btcusdt.bid) * (1 - fee)

    profit_usdt = usdt_final - start_usdt
    profit_pct = profit_usdt / start_usdt

    return ArbResult(
        direction="reverse (USDT->ETH->BTC->USDT)",
        start_usdt=start_usdt,
        end_usdt=usdt_final,
        profit_pct=profit_pct,
        profit_usdt=profit_usdt,
        is_opportunity=profit_pct > config.MIN_PROFIT_THRESHOLD,
    )


def check_both_directions(btcusdt: BookTicker, ethbtc: BookTicker, ethusdt: BookTicker):
    """Run both loop directions and return both results."""
    fwd = forward_loop(btcusdt, ethbtc, ethusdt, config.SIMULATED_START_USDT, config.TAKER_FEE)
    rev = reverse_loop(btcusdt, ethbtc, ethusdt, config.SIMULATED_START_USDT, config.TAKER_FEE)
    return fwd, rev
