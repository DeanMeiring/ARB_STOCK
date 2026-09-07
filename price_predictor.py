"""
On-demand prediction using whatever model analyze_price_trend_model.py last
saved to Postgres. Deliberately separate from that training script and only
imported inside telegram_bot's callback handler (not at module load time) -
pandas/xgboost's import cost should only be paid when someone actually asks
for a prediction, never in the always-on detection hot path.
"""

import pickle
import numpy as np
import pandas as pd
import psycopg2
import config
import logger
from analyze_price_trend_model import SYMBOL, MODEL_NAME, FEATURE_COLS


def predict_latest() -> str:
    saved = logger.load_model(MODEL_NAME)
    if not saved:
        return "No trained model yet - the daily training job hasn't produced one. Check back after it's run at least once (06:00 UTC)."
    blob, metadata, trained_at = saved
    model = pickle.loads(blob)

    conn = psycopg2.connect(config.DATABASE_URL)
    df = pd.read_sql(
        "SELECT candle_start, close FROM market_candles WHERE symbol = %(symbol)s "
        "ORDER BY candle_start DESC LIMIT 30",
        conn, params={"symbol": SYMBOL}, parse_dates=["candle_start"],
    )
    conn.close()

    if len(df) < 20:
        return "Not enough recent candle data to predict yet."

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
        return "Not enough recent history to compute features yet."

    prob_up = float(model.predict_proba(latest)[0][1])
    confidence = max(prob_up, 1 - prob_up)
    action = "BUY" if prob_up > 0.5 else "SHORT"
    auc = (metadata or {}).get("auc")
    auc_str = f" (test AUC {auc:.3f} when trained)" if auc is not None else ""

    return (
        f"🔮 {SYMBOL}: {confidence*100:.0f}% confidence to {action} here{auc_str}\n"
        f"Model last trained {trained_at:%b %-d, %H:%M} UTC\n"
        f"(rough statistical model on limited data - not financial advice)"
    )
