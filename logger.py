"""
Postgres logging for detected arbitrage opportunities and Telegram subscribers.

This is the "evidence" layer - every time the calculator finds a loop that
clears the profit threshold, we log it here with full price context. Over
time this becomes a real dataset you can analyze: how often do opportunities
appear, how big are they, do they cluster around volatility spikes, etc.

Uses config.DATABASE_URL (Railway's Postgres add-on, injected automatically).
"""

from datetime import datetime, timezone
import psycopg2
import config


def _connect():
    return psycopg2.connect(config.DATABASE_URL)


def init_db():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ NOT NULL,
            direction TEXT NOT NULL,
            btcusdt_bid DOUBLE PRECISION, btcusdt_ask DOUBLE PRECISION,
            ethbtc_bid DOUBLE PRECISION, ethbtc_ask DOUBLE PRECISION,
            ethusdt_bid DOUBLE PRECISION, ethusdt_ask DOUBLE PRECISION,
            start_usdt DOUBLE PRECISION,
            end_usdt DOUBLE PRECISION,
            profit_pct DOUBLE PRECISION,
            profit_usdt DOUBLE PRECISION
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS telegram_subscribers (
            chat_id BIGINT PRIMARY KEY,
            authorized_at TIMESTAMPTZ NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def log_opportunity(result, btcusdt, ethbtc, ethusdt):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO opportunities (
            timestamp, direction,
            btcusdt_bid, btcusdt_ask,
            ethbtc_bid, ethbtc_ask,
            ethusdt_bid, ethusdt_ask,
            start_usdt, end_usdt, profit_pct, profit_usdt
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        datetime.now(timezone.utc),
        result.direction,
        btcusdt.bid, btcusdt.ask,
        ethbtc.bid, ethbtc.ask,
        ethusdt.bid, ethusdt.ask,
        result.start_usdt, result.end_usdt,
        result.profit_pct, result.profit_usdt,
    ))
    conn.commit()
    cur.close()
    conn.close()


def add_subscriber(chat_id: int):
    """Record a chat_id as authorized after a successful /login."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO telegram_subscribers (chat_id, authorized_at)
        VALUES (%s, %s)
        ON CONFLICT (chat_id) DO NOTHING
    """, (chat_id, datetime.now(timezone.utc)))
    conn.commit()
    cur.close()
    conn.close()


def get_subscribers() -> list:
    """Return every chat_id authorized to receive opportunity alerts."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM telegram_subscribers")
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def get_report_stats() -> dict:
    """Summary stats for the /report Telegram command."""
    conn = _connect()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM opportunities")
    total = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM opportunities
        WHERE timestamp > NOW() - INTERVAL '24 hours'
    """)
    last_24h = cur.fetchone()[0]

    cur.execute("""
        SELECT direction, profit_pct, profit_usdt, timestamp
        FROM opportunities ORDER BY timestamp DESC LIMIT 1
    """)
    latest = cur.fetchone()

    cur.execute("""
        SELECT direction, profit_pct, profit_usdt, timestamp
        FROM opportunities ORDER BY profit_pct DESC LIMIT 1
    """)
    best = cur.fetchone()

    cur.execute("SELECT COUNT(*) FROM telegram_subscribers")
    subscribers = cur.fetchone()[0]

    cur.close()
    conn.close()
    return {
        "total": total,
        "last_24h": last_24h,
        "latest": latest,
        "best": best,
        "subscribers": subscribers,
    }
