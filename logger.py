"""
Postgres logging for detected arbitrage opportunities and Telegram subscribers.

This is the "evidence" layer - every time a calculator finds a loop (or a
cross-exchange leg) that clears the profit threshold, we log it here with
full price context. Over time this becomes a real dataset you can analyze:
how often do opportunities appear, how big are they, do they cluster around
volatility spikes, etc.

Every opportunity/near-miss row carries a `source` - 'triangular' (the
original Binance-only BTC/ETH/USDT loop) or 'cross_exchange' (Binance vs
Crypto.com) - so the two detectors' results stay clearly separated.

Uses config.DATABASE_URL (Railway's Postgres add-on, injected automatically).
"""

from datetime import datetime, timezone
import psycopg2
import psycopg2.extras
from psycopg2.extras import Json
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
    # ADD COLUMN IF NOT EXISTS so this stays safe to run against the
    # already-populated production table, not just a fresh one.
    cur.execute("ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'triangular'")
    cur.execute("ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS cryptocom_bid DOUBLE PRECISION")
    cur.execute("ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS cryptocom_ask DOUBLE PRECISION")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS telegram_subscribers (
            chat_id BIGINT PRIMARY KEY,
            authorized_at TIMESTAMPTZ NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS near_misses (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ NOT NULL,
            direction TEXT NOT NULL,
            profit_pct DOUBLE PRECISION,
            profit_usdt DOUBLE PRECISION
        )
    """)
    cur.execute("ALTER TABLE near_misses ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'triangular'")

    # 1-minute OHLC candles built from mid-price ((bid+ask)/2), one row per
    # symbol per minute - this is the actual price history for training a
    # trend model later. Not written per-tick (hundreds/sec); main.py builds
    # each candle in memory and flushes it once per minute.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_candles (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            candle_start TIMESTAMPTZ NOT NULL,
            open DOUBLE PRECISION, high DOUBLE PRECISION,
            low DOUBLE PRECISION, close DOUBLE PRECISION,
            tick_count INTEGER,
            UNIQUE (symbol, candle_start)
        )
    """)

    # Trained model artifacts - Railway's filesystem doesn't persist across
    # deploys/restarts, so the serialized model lives here instead of on
    # disk. One row per named model, overwritten on each retrain.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trained_models (
            model_name TEXT PRIMARY KEY,
            trained_at TIMESTAMPTZ NOT NULL,
            model_blob BYTEA NOT NULL,
            metadata JSONB
        )
    """)

    conn.commit()
    cur.close()
    conn.close()


def log_opportunity(result, source, btcusdt=None, ethbtc=None, ethusdt=None, cryptocom=None):
    """
    source: 'triangular' or 'cross_exchange'.
    Pass btcusdt/ethbtc/ethusdt for a triangular result, or btcusdt/cryptocom
    for a cross-exchange one - whichever don't apply stay NULL in the row.
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO opportunities (
            timestamp, direction, source,
            btcusdt_bid, btcusdt_ask,
            ethbtc_bid, ethbtc_ask,
            ethusdt_bid, ethusdt_ask,
            cryptocom_bid, cryptocom_ask,
            start_usdt, end_usdt, profit_pct, profit_usdt
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        datetime.now(timezone.utc),
        result.direction,
        source,
        btcusdt.bid if btcusdt else None, btcusdt.ask if btcusdt else None,
        ethbtc.bid if ethbtc else None, ethbtc.ask if ethbtc else None,
        ethusdt.bid if ethusdt else None, ethusdt.ask if ethusdt else None,
        cryptocom.bid if cryptocom else None, cryptocom.ask if cryptocom else None,
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


def save_model(model_name: str, blob: bytes, metadata: dict = None):
    """Overwrite the stored artifact for model_name with a freshly trained one."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO trained_models (model_name, trained_at, model_blob, metadata)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (model_name) DO UPDATE SET
            trained_at = EXCLUDED.trained_at,
            model_blob = EXCLUDED.model_blob,
            metadata = EXCLUDED.metadata
    """, (model_name, datetime.now(timezone.utc), psycopg2.Binary(blob), Json(metadata) if metadata else None))
    conn.commit()
    cur.close()
    conn.close()


def load_model(model_name: str):
    """Returns (blob: bytes, metadata: dict, trained_at) or None if never trained."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT model_blob, metadata, trained_at FROM trained_models WHERE model_name = %s", (model_name,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    blob, metadata, trained_at = row
    return bytes(blob), metadata, trained_at


def log_near_miss(direction: str, profit_pct: float, profit_usdt: float, source: str = "triangular"):
    """
    Record the best (highest profit_pct) sub-threshold result seen in a
    sampling window - see main.py's periodic flush. Not written per-tick,
    that would be hundreds of writes/sec for no real benefit.
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO near_misses (timestamp, direction, profit_pct, profit_usdt, source)
        VALUES (%s, %s, %s, %s, %s)
    """, (datetime.now(timezone.utc), direction, profit_pct, profit_usdt, source))
    conn.commit()
    cur.close()
    conn.close()


def get_closest_miss_24h(source: str = "triangular"):
    """Best (highest profit_pct) sampled near-miss in the last 24h for this source, or None."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT direction, profit_pct, profit_usdt, timestamp
        FROM near_misses
        WHERE timestamp > NOW() - INTERVAL '24 hours' AND source = %s
        ORDER BY profit_pct DESC LIMIT 1
    """, (source,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def get_report_stats(source: str = "triangular") -> dict:
    """Summary stats for the /report Telegram command, scoped to one source."""
    conn = _connect()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM opportunities WHERE source = %s", (source,))
    total = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM opportunities
        WHERE timestamp > NOW() - INTERVAL '24 hours' AND source = %s
    """, (source,))
    last_24h = cur.fetchone()[0]

    cur.execute("""
        SELECT direction, profit_pct, profit_usdt, timestamp
        FROM opportunities WHERE source = %s ORDER BY timestamp DESC LIMIT 1
    """, (source,))
    latest = cur.fetchone()

    cur.execute("""
        SELECT direction, profit_pct, profit_usdt, timestamp
        FROM opportunities WHERE source = %s ORDER BY profit_pct DESC LIMIT 1
    """, (source,))
    best = cur.fetchone()

    cur.close()
    conn.close()
    return {
        "total": total,
        "last_24h": last_24h,
        "latest": latest,
        "best": best,
    }


def log_candle(symbol: str, candle_start, open_: float, high: float, low: float, close: float, tick_count: int):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO market_candles (symbol, candle_start, open, high, low, close, tick_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (symbol, candle_start) DO NOTHING
    """, (symbol, candle_start, open_, high, low, close, tick_count))
    conn.commit()
    cur.close()
    conn.close()


def bulk_log_candles(rows: list):
    """
    rows: list of (symbol, candle_start, open, high, low, close, tick_count)
    tuples. For backfilling thousands of historical candles at once - one
    connection for the whole batch instead of one per row.
    """
    if not rows:
        return
    conn = _connect()
    cur = conn.cursor()
    psycopg2.extras.execute_values(cur, """
        INSERT INTO market_candles (symbol, candle_start, open, high, low, close, tick_count)
        VALUES %s
        ON CONFLICT (symbol, candle_start) DO NOTHING
    """, rows, page_size=1000)
    conn.commit()
    cur.close()
    conn.close()
