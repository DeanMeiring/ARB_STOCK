"""
Trains an XGBoost classifier predicting whether BTCUSDT's next 1-minute
candle closes higher than the current one, using market_candles history
(the 30-day backfill plus whatever's accumulated live since).

The trained model is saved to Postgres (logger.save_model), not disk -
Railway's filesystem doesn't persist across deploys/restarts. price_predictor.py
is the lightweight counterpart that loads it back for on-demand predictions
from the live bot (via Telegram's /predict), kept in a separate file so the
always-on detector doesn't pay pandas/xgboost's import cost unless someone
actually asks for a prediction.

Standalone - called from analyze_opportunity_model.py's main() as part of
the same daily cron run, not scheduled separately.
"""

import pickle
import numpy as np
import pandas as pd
import psycopg2
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score
import xgboost as xgb
import config
import logger

SYMBOL = "BTCUSDT"
MODEL_NAME = "price_trend_btcusdt"
MIN_ROWS = 500

# Shared with price_predictor.py's inference path - keep both in sync if
# either changes, they must compute features identically.
FEATURE_COLS = ["return_1", "return_5", "return_15", "volatility_15", "hour_sin", "hour_cos", "day_of_week"]


def load_candles(symbol: str) -> pd.DataFrame:
    conn = psycopg2.connect(config.DATABASE_URL)
    df = pd.read_sql(
        "SELECT candle_start, close FROM market_candles WHERE symbol = %(symbol)s ORDER BY candle_start",
        conn, params={"symbol": symbol}, parse_dates=["candle_start"],
    )
    conn.close()
    # same empty-vs-populated dtype gotcha as analyze_opportunity_model.py
    df["candle_start"] = pd.to_datetime(df["candle_start"], utc=True)
    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["return_1"] = df["close"].pct_change(1)
    df["return_5"] = df["close"].pct_change(5)
    df["return_15"] = df["close"].pct_change(15)
    df["volatility_15"] = df["return_1"].rolling(15).std()
    df["hour_sin"] = np.sin(2 * np.pi * df["candle_start"].dt.hour / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df["candle_start"].dt.hour / 24)
    df["day_of_week"] = df["candle_start"].dt.dayofweek
    # target: does the NEXT candle close higher than this one?
    df["target"] = (df["close"].shift(-1) > df["close"]).astype(int)
    df = df.dropna().reset_index(drop=True)
    return df


def train() -> str:
    df = load_candles(SYMBOL)
    if len(df) < MIN_ROWS:
        return f"{SYMBOL} price-trend: only {len(df)} candles so far (need {MIN_ROWS}+) - too early to train."

    df = build_features(df)
    X, y = df[FEATURE_COLS], df["target"]
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, shuffle=False)

    model = xgb.XGBClassifier(n_estimators=150, max_depth=4, eval_metric="logloss")
    model.fit(X_train, y_train)

    y_prob = model.predict_proba(X_test)[:, 1]
    try:
        auc = roc_auc_score(y_test, y_prob)
        # some sklearn versions return NaN instead of raising for a
        # degenerate split (e.g. the test slice is all one class), rather
        # than the ValueError the except below expects - NaN isn't valid
        # JSON either, so it'd otherwise break the Postgres write below.
        if np.isnan(auc):
            auc, auc_str = None, "n/a (degenerate test split)"
        else:
            auc_str = f"{auc:.3f}"
    except ValueError:
        auc, auc_str = None, "n/a (test set has no positive examples)"

    blob = pickle.dumps(model)
    logger.save_model(MODEL_NAME, blob, {"auc": auc, "rows": len(df), "features": FEATURE_COLS})

    up_rate = df["target"].mean()
    return (
        f"{SYMBOL} price-trend: {len(df)} candles, {up_rate*100:.1f}% closed up historically.\n"
        f"  Test AUC: {auc_str} (0.5 = no better than chance)\n"
        f"  Model saved - available via /predict in Telegram."
    )


if __name__ == "__main__":
    print(train())
