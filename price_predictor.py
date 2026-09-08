"""
On-demand prediction using whatever models analyze_price_trend_model.py last
saved to Postgres - one per coin in config.PREDICT_SYMBOLS. Deliberately
separate from that training script and only imported inside telegram_bot's
handler (not at module load time) - pandas/xgboost's import cost should only
be paid when someone actually asks for a prediction, never in the always-on
detection hot path.
"""

import pickle
import numpy as np
import pandas as pd
import psycopg2
import config
import logger
from analyze_price_trend_model import FEATURE_COLS, model_name


def predict_symbol(symbol: str):
    """
    Returns a dict {symbol, prob_up, trained_at, auc, accuracy, price} on success,
    or a plain string explaining why this symbol has no prediction right now
    (no trained model yet / not enough recent candles).
    """
    saved = logger.load_model(model_name(symbol))
    if not saved:
        return f"{symbol}: no trained model yet"
    blob, metadata, trained_at = saved
    model = pickle.loads(blob)

    conn = psycopg2.connect(config.DATABASE_URL)
    df = pd.read_sql(
        "SELECT candle_start, close FROM market_candles WHERE symbol = %(symbol)s "
        "ORDER BY candle_start DESC LIMIT 30",
        conn, params={"symbol": symbol}, parse_dates=["candle_start"],
    )
    conn.close()

    if len(df) < 20:
        return f"{symbol}: not enough recent candle data yet"

    df["candle_start"] = pd.to_datetime(df["candle_start"], utc=True)
    df = df.sort_values("candle_start").reset_index(drop=True)
    df["return_1"] = df["close"].pct_change(1)
    df["return_5"] = df["close"].pct_change(5)
    df["return_15"] = df["close"].pct_change(15)
    df["volatility_15"] = df["return_1"].rolling(15).std()
    df["hour_sin"] = np.sin(2 * np.pi * df["candle_start"].dt.hour / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df["candle_start"].dt.hour / 24)
    df["day_of_week"] = df["candle_start"].dt.dayofweek

    latest = df.iloc[[-1]][FEATURE_COLS]
    if latest.isnull().any(axis=1).iloc[0]:
        return f"{symbol}: not enough recent history to compute features yet"

    prob_up = float(model.predict_proba(latest)[0][1])
    metadata = metadata or {}
    return {
        "symbol": symbol, "prob_up": prob_up, "trained_at": trained_at,
        "auc": metadata.get("auc"), "accuracy": metadata.get("accuracy"),
        "price": float(df.iloc[-1]["close"]),
    }


def predict_all() -> list:
    """[{symbol, prob_up, trained_at, auc, accuracy}, ...] for every symbol with a usable model, sorted by prob_up desc."""
    results = [predict_symbol(s) for s in config.PREDICT_SYMBOLS]
    ok = [r for r in results if isinstance(r, dict)]
    ok.sort(key=lambda r: r["prob_up"], reverse=True)
    return ok


def predict_all_text() -> str:
    """Formatted Telegram message: every coin's up-probability, with coins
    scoring >= config.PREDICT_UP_THRESHOLD called out explicitly."""
    results = [predict_symbol(s) for s in config.PREDICT_SYMBOLS]
    ok = sorted((r for r in results if isinstance(r, dict)), key=lambda r: r["prob_up"], reverse=True)
    unavailable = [r for r in results if isinstance(r, str)]

    if not ok:
        return "No trained models available yet - the daily training job hasn't produced one. Check back after it's run (06:00 UTC)."

    lines = ["🔮 Price-trend predictions (next hour, probability of going up):"]
    for r in ok:
        marker = " ⬆️" if r["prob_up"] >= config.PREDICT_UP_THRESHOLD else ""
        accuracy_str = f", model accuracy {r['accuracy']*100:.0f}%" if r.get("accuracy") is not None else ""
        lines.append(f"  {r['symbol']}: {r['prob_up']*100:.0f}%{marker}{accuracy_str}")

    hits = [r for r in ok if r["prob_up"] >= config.PREDICT_UP_THRESHOLD]
    if hits:
        lines.append(
            f"\n{len(hits)} coin(s) >= {config.PREDICT_UP_THRESHOLD*100:.0f}%: "
            + ", ".join(r["symbol"] for r in hits)
        )
    else:
        lines.append(f"\nNo coins currently >= {config.PREDICT_UP_THRESHOLD*100:.0f}% probability up.")

    if unavailable:
        lines.append("\n" + "\n".join(unavailable))

    lines.append("\n(rough statistical models on limited data - not financial advice)")
    return "\n".join(lines)
