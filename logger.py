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

    # One row per training-cron run (success or failure) - Railway's get-logs
    # has repeatedly come back empty for this service's quick one-off
    # deploys, and the cron service's own Telegram send can silently no-op
    # if its TELEGRAM_API_BOT var is missing/wrong (a separate var from the
    # main bot's - Railway doesn't share plain env vars between services).
    # Postgres always works when the script got far enough to reach it, and
    # /trainstatus (served by the main bot, whose Telegram config is proven
    # working) reads it back - a diagnostic channel that doesn't depend on
    # anything specific to the cron service.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS training_runs (
            id SERIAL PRIMARY KEY,
            ran_at TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL,
            detail TEXT
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


def log_training_run(status: str, detail: str = None):
    """status: 'success' or 'failed'. One row per run of analyze_opportunity_model.py."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO training_runs (ran_at, status, detail)
        VALUES (%s, %s, %s)
    """, (datetime.now(timezone.utc), status, detail))
    conn.commit()
    cur.close()
    conn.close()


def get_latest_training_run():
    """Returns (ran_at, status, detail) for the most recent training run, or None if it's never run."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT ran_at, status, detail FROM training_runs ORDER BY ran_at DESC LIMIT 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


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


def get_recent_opportunities(hours: int = 24, source: str = None) -> list:
    """JSON-friendly rows for the dashboard's opportunities chart."""
    conn = _connect()
    cur = conn.cursor()
    if source:
        cur.execute("""
            SELECT timestamp, direction, source, profit_pct, profit_usdt
            FROM opportunities
            WHERE timestamp > NOW() - make_interval(hours => %s) AND source = %s
            ORDER BY timestamp ASC
        """, (hours, source))
    else:
        cur.execute("""
            SELECT timestamp, direction, source, profit_pct, profit_usdt
            FROM opportunities
            WHERE timestamp > NOW() - make_interval(hours => %s)
            ORDER BY timestamp ASC
        """, (hours,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {"timestamp": ts.isoformat(), "direction": direction, "source": src, "profit_pct": pct, "profit_usdt": usdt}
        for ts, direction, src, pct, usdt in rows
    ]


def get_recent_near_misses(hours: int = 24, source: str = None) -> list:
    """JSON-friendly rows for the dashboard's near-miss trend chart."""
    conn = _connect()
    cur = conn.cursor()
    if source:
        cur.execute("""
            SELECT timestamp, direction, source, profit_pct, profit_usdt
            FROM near_misses
            WHERE timestamp > NOW() - make_interval(hours => %s) AND source = %s
            ORDER BY timestamp ASC
        """, (hours, source))
    else:
        cur.execute("""
            SELECT timestamp, direction, source, profit_pct, profit_usdt
            FROM near_misses
            WHERE timestamp > NOW() - make_interval(hours => %s)
            ORDER BY timestamp ASC
        """, (hours,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {"timestamp": ts.isoformat(), "direction": direction, "source": src, "profit_pct": pct, "profit_usdt": usdt}
        for ts, direction, src, pct, usdt in rows
    ]


def get_recent_candles(symbol: str, hours: int = 24) -> list:
    """JSON-friendly close-price rows for the dashboard's price chart."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT candle_start, close
        FROM market_candles
        WHERE symbol = %s AND candle_start > NOW() - make_interval(hours => %s)
        ORDER BY candle_start ASC
    """, (symbol, hours))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{"timestamp": ts.isoformat(), "close": close} for ts, close in rows]


def get_recent_candles_multi(symbols: list, hours: int = 24) -> dict:
    """Same as get_recent_candles, for several symbols in one query - used by
    the dashboard's multi-coin signal widget instead of one request per coin."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, candle_start, close
        FROM market_candles
        WHERE symbol = ANY(%s) AND candle_start > NOW() - make_interval(hours => %s)
        ORDER BY candle_start ASC
    """, (symbols, hours))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    result = {symbol: [] for symbol in symbols}
    for symbol, ts, close in rows:
        result[symbol].append({"timestamp": ts.isoformat(), "close": close})
    return result


def get_trained_models_json() -> list:
    """One row per model in trained_models, JSON-friendly, no blob included."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT model_name, trained_at, metadata FROM trained_models ORDER BY model_name")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {"model_name": name, "trained_at": trained_at.isoformat(), "metadata": metadata}
        for name, trained_at, metadata in rows
    ]


def get_stats_json() -> dict:
    """Everything the dashboard's stat tiles need, in one call."""

    def _row(row):
        if not row:
            return None
        direction, profit_pct, profit_usdt, ts = row
        return {"direction": direction, "profit_pct": profit_pct, "profit_usdt": profit_usdt, "timestamp": ts.isoformat()}

    def _source_stats(source):
        s = get_report_stats(source)
        return {"total": s["total"], "last_24h": s["last_24h"], "latest": _row(s["latest"]), "best": _row(s["best"])}

    training_run = get_latest_training_run()
    training_run_json = None
    if training_run:
        ran_at, status, detail = training_run
        training_run_json = {"ran_at": ran_at.isoformat(), "status": status, "detail": detail}

    return {
        "triangular": _source_stats("triangular"),
        "cross_exchange": _source_stats("cross_exchange"),
        "subscribers": len(get_subscribers()),
        "latest_training_run": training_run_json,
    }


def init_trading_tables():
    """
    Real trade executions (executor.py) and the governor's kill-switch state.
    Postgres, not the old sqlite3 DB_PATH file - Railway's filesystem doesn't
    persist across deploys/restarts, which would silently reset the daily
    loss limit (and lose trade history) on every redeploy.
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ NOT NULL,
            direction TEXT NOT NULL,
            expected_profit_pct DOUBLE PRECISION,
            status TEXT NOT NULL,              -- 'success', 'failed_unwound', 'failed_stuck'
            start_usdt DOUBLE PRECISION,
            end_usdt DOUBLE PRECISION,
            profit_usdt DOUBLE PRECISION,      -- NULL when not cleanly computable in USDT terms (see executor.py) - treat as unknown/needs review, never as zero
            leg1_symbol TEXT, leg1_side TEXT, leg1_result JSONB,
            leg2_symbol TEXT, leg2_side TEXT, leg2_result JSONB,
            leg3_symbol TEXT, leg3_side TEXT, leg3_result JSONB,
            error TEXT
        )
    """)
    # Singleton row (id always 1) - current kill-switch state. Starts unset
    # (no row) = not killed; set_kill_switch upserts it.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS governor_state (
            id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            killed BOOLEAN NOT NULL,
            reason TEXT,
            updated_at TIMESTAMPTZ NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def init_prediction_tracking():
    """
    prediction_log: every price_predictor call gets recorded here (see
    prediction_tracker.py), then scored once its holding window elapses -
    this is what /predict and the dashboard's "prediction accuracy today"
    numbers are computed from. Pure paper tracking, no relation to
    trades/governor_state above - no real order is ever involved.
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS prediction_log (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            predicted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            prob_up DOUBLE PRECISION NOT NULL,
            price_at_prediction DOUBLE PRECISION NOT NULL,
            resolve_at TIMESTAMPTZ NOT NULL,
            resolved BOOLEAN NOT NULL DEFAULT FALSE,
            price_at_resolution DOUBLE PRECISION,
            correct BOOLEAN,
            pnl_usdt DOUBLE PRECISION
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def log_prediction(symbol: str, prob_up: float, price: float):
    """Records one price_predictor call for later scoring - see
    prediction_tracker.log_due_predictions()."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO prediction_log (symbol, prob_up, price_at_prediction, resolve_at)
        VALUES (%s, %s, %s, NOW() + make_interval(mins => %s))
    """, (symbol, prob_up, price, config.PREDICTION_HORIZON_MINUTES))
    conn.commit()
    cur.close()
    conn.close()


def get_due_predictions() -> list:
    """Unresolved predictions whose holding window has elapsed - ready to be scored."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, symbol, price_at_prediction, prob_up
        FROM prediction_log
        WHERE NOT resolved AND resolve_at <= NOW()
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_latest_close(symbol: str):
    """Most recent market_candles close for symbol, or None if there's no candle yet."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT close FROM market_candles WHERE symbol = %s ORDER BY candle_start DESC LIMIT 1", (symbol,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def resolve_prediction(pred_id: int, price_at_resolution: float, correct: bool, pnl_usdt: float):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        UPDATE prediction_log
        SET resolved = TRUE, price_at_resolution = %s, correct = %s, pnl_usdt = %s
        WHERE id = %s
    """, (price_at_resolution, correct, pnl_usdt, pred_id))
    conn.commit()
    cur.close()
    conn.close()


def get_todays_prediction_stats() -> dict:
    """
    Resolved-only tally of price_predictor's calls, for "today" in SAST
    (South Africa Standard Time, UTC+2, no DST) - matching the dashboard's
    display timezone, unlike get_todays_trade_stats' plain UTC day (a
    separate, pre-existing convention for the live-trading governor that
    this doesn't touch). Unresolved (still within their holding window)
    calls aren't counted yet - they show up once resolved.
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*), COALESCE(SUM(CASE WHEN correct THEN 1 ELSE 0 END), 0), COALESCE(SUM(pnl_usdt), 0)
        FROM prediction_log
        WHERE resolved
          AND predicted_at > date_trunc('day', NOW() AT TIME ZONE 'Africa/Johannesburg') AT TIME ZONE 'Africa/Johannesburg'
    """)
    total, correct_count, pnl_usdt = cur.fetchone()
    cur.close()
    conn.close()
    return {
        "total": total,
        "correct": correct_count,
        "accuracy_pct": (correct_count / total * 100) if total else None,
        "pnl_usdt": pnl_usdt,
    }


def log_trade(direction: str, expected_profit_pct: float, status: str, legs: list,
              start_usdt: float = None, end_usdt: float = None, profit_usdt: float = None, error: str = None):
    """
    legs: [[symbol, side, result_dict_or_None], ...] x3, result is the raw
    Binance order response (or None if that leg never got placed).
    profit_usdt: pass None when it can't be cleanly computed in USDT terms
    (see executor.py's unwind paths) - the governor treats a NULL-profit
    trade as needing manual review, never as a zero/neutral outcome.
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO trades (
            timestamp, direction, expected_profit_pct, status, start_usdt, end_usdt, profit_usdt,
            leg1_symbol, leg1_side, leg1_result,
            leg2_symbol, leg2_side, leg2_result,
            leg3_symbol, leg3_side, leg3_result,
            error
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        datetime.now(timezone.utc), direction, expected_profit_pct, status, start_usdt, end_usdt, profit_usdt,
        legs[0][0], legs[0][1], Json(legs[0][2]) if legs[0][2] is not None else None,
        legs[1][0], legs[1][1], Json(legs[1][2]) if legs[1][2] is not None else None,
        legs[2][0], legs[2][1], Json(legs[2][2]) if legs[2][2] is not None else None,
        error,
    ))
    conn.commit()
    cur.close()
    conn.close()


def get_todays_trade_stats() -> dict:
    """
    count: trades attempted today (any status).
    known_profit_usdt: sum of profit_usdt for trades where it's known -
    excludes NULL-profit (needs-review) trades, so this can UNDERSTATE the
    real loss if one of those is actually a big loser. That's intentional:
    the governor treats any NULL-profit trade as an automatic kill-switch
    trigger (see governor.py), so it never relies on this number alone.
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*), COALESCE(SUM(profit_usdt), 0)
        FROM trades
        WHERE timestamp > date_trunc('day', NOW())
    """)
    count, known_profit_usdt = cur.fetchone()
    cur.execute("""
        SELECT COUNT(*) FROM trades
        WHERE timestamp > date_trunc('day', NOW()) AND profit_usdt IS NULL
    """)
    unknown_count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return {"count": count, "known_profit_usdt": known_profit_usdt, "unknown_profit_count": unknown_count}


def get_cumulative_profit_usdt() -> float:
    """Sum of profit_usdt across all known-outcome trades, ever. Same NULL caveat as get_todays_trade_stats."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(profit_usdt), 0) FROM trades")
    total = cur.fetchone()[0]
    cur.close()
    conn.close()
    return total


def get_last_trade_time():
    """Timestamp of the most recent trade attempt (any status), or None if there's never been one."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT timestamp FROM trades ORDER BY timestamp DESC LIMIT 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def get_kill_switch() -> dict:
    """{'killed': bool, 'reason': str|None, 'updated_at': datetime|None} - killed=False if never set."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT killed, reason, updated_at FROM governor_state WHERE id = 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return {"killed": False, "reason": None, "updated_at": None}
    killed, reason, updated_at = row
    return {"killed": killed, "reason": reason, "updated_at": updated_at}


def set_kill_switch(killed: bool, reason: str = None):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO governor_state (id, killed, reason, updated_at)
        VALUES (1, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET killed = EXCLUDED.killed, reason = EXCLUDED.reason, updated_at = EXCLUDED.updated_at
    """, (killed, reason, datetime.now(timezone.utc)))
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
