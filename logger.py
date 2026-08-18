"""
SQLite logging for detected arbitrage opportunities.

This is the "evidence" layer - every time the calculator finds a loop that
clears the profit threshold, we log it here with full price context. Over
time this becomes a real dataset you can analyze: how often do opportunities
appear, how big are they, do they cluster around volatility spikes, etc.
"""

import sqlite3
from datetime import datetime, timezone
import config


def init_db():
    conn = sqlite3.connect(config.DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            direction TEXT NOT NULL,
            btcusdt_bid REAL, btcusdt_ask REAL,
            ethbtc_bid REAL, ethbtc_ask REAL,
            ethusdt_bid REAL, ethusdt_ask REAL,
            start_usdt REAL,
            end_usdt REAL,
            profit_pct REAL,
            profit_usdt REAL
        )
    """)
    conn.commit()
    conn.close()


def log_opportunity(result, btcusdt, ethbtc, ethusdt):
    conn = sqlite3.connect(config.DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO opportunities (
            timestamp, direction,
            btcusdt_bid, btcusdt_ask,
            ethbtc_bid, ethbtc_ask,
            ethusdt_bid, ethusdt_ask,
            start_usdt, end_usdt, profit_pct, profit_usdt
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        result.direction,
        btcusdt.bid, btcusdt.ask,
        ethbtc.bid, ethbtc.ask,
        ethusdt.bid, ethusdt.ask,
        result.start_usdt, result.end_usdt,
        result.profit_pct, result.profit_usdt,
    ))
    conn.commit()
    conn.close()
