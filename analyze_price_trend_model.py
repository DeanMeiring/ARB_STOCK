"""
Trains an XGBoost classifier per coin (config.PREDICT_SYMBOLS) predicting
whether its next 1-minute candle closes higher than the current one, using
market_candles history.

All of config.PREDICT_SYMBOLS get organic candle collection from the live WS
stream in main.py, but that alone would mean a freshly-added coin trains on
whatever thin window happened to accumulate since it was added. Every
training run instead tops up each symbol to a full BACKFILL_DAYS-deep
history first (ensure_backfilled, reusing backfill_candles.fetch_binance_klines
- the same Binance historical-klines endpoint backfill_candles.py's one-off
script uses), so a new coin gets a usable model on its very first run, and
an existing one keeps training on the full configured window even if
BACKFILL_DAYS gets raised later.

Each trained model is saved to Postgres (logger.save_model) - Railway's
filesystem doesn't persist across deploys/restarts - as its own row, keyed
by model_name(symbol), not one shared blob. price_predictor.py is the
lightweight counterpart that loads them back for on-demand predictions from
the live bot (via Telegram's /predict), kept in a separate file so the
always-on detector doesn't pay pandas/xgboost's import cost unless someone
actually asks for a prediction.

Standalone - called from analyze_opportunity_model.py's main() as part of
the same daily cron run, not scheduled separately.
"""

from datetime import datetime, timedelta, timezone
import pickle
import numpy as np
import pandas as pd
import psycopg2
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, accuracy_score
import xgboost as xgb
import config
import logger
from backfill_candles import fetch_binance_klines, BACKFILL_DAYS

MIN_ROWS = 500

# Shared with price_predictor.py's inference path - keep both in sync if
# either changes, they must compute features identically.
FEATURE_COLS = ["return_1", "return_5", "return_15", "volatility_15", "hour_sin", "hour_cos", "day_of_week"]


def model_name(symbol: str) -> str:
    return f"price_trend_{symbol.lower()}"


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


def ensure_backfilled(symbol: str, df: pd.DataFrame):
    """
    Tops up history via Binance's historical klines endpoint whenever this
    symbol's oldest candle doesn't yet reach back BACKFILL_DAYS - not just
    the first time (a brand-new symbol with zero rows), but every training
    run, so a coin that only ever got an earlier, shorter backfill (e.g.
    before BACKFILL_DAYS was raised) catches up too. Idempotent - inserts
    use ON CONFLICT DO NOTHING (see logger.bulk_log_candles), so re-running
    this against a symbol that's already fully backfilled just fetches an
    empty/tiny gap and no-ops.
    """
    end = datetime.now(timezone.utc)
    desired_start = end - timedelta(days=BACKFILL_DAYS)
    earliest = df["candle_start"].min() if len(df) else None

    if earliest is not None and earliest <= desired_start + timedelta(hours=1):
        return  # already covers the full desired window

    fetch_end = earliest if earliest is not None else end
    rows = fetch_binance_klines(symbol, int(desired_start.timestamp() * 1000), int(fetch_end.timestamp() * 1000))
    logger.bulk_log_candles(rows)


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


def train_symbol(symbol: str) -> str:
    df = load_candles(symbol)
    ensure_backfilled(symbol, df)
    df = load_candles(symbol)  # re-read - picks up whatever ensure_backfilled just topped up, no-op otherwise

    if len(df) < MIN_ROWS:
        return f"{symbol}: only {len(df)} candles so far (need {MIN_ROWS}+) - too early to train."

    df = build_features(df)
    X, y = df[FEATURE_COLS], df["target"]
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, shuffle=False)

    model = xgb.XGBClassifier(n_estimators=150, max_depth=4, eval_metric="logloss")
    model.fit(X_train, y_train)

    y_prob = model.predict_proba(X_test)[:, 1]
    y_pred = model.predict(X_test)
    # accuracy is % of test-set predictions correct at the model's default
    # 50% cutoff - easier to read than AUC, but tells you less: it doesn't
    # reward correctly-ranked confidence, and on an imbalanced test set a
    # trivial "always predict the majority class" model can score high
    # accuracy while still being useless. Report both, not one or the other.
    accuracy = accuracy_score(y_test, y_pred)
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
    logger.save_model(model_name(symbol), blob, {
        "auc": auc, "accuracy": accuracy, "rows": len(df), "features": FEATURE_COLS,
    })

    up_rate = df["target"].mean()
    return (f"{symbol}: {len(df)} candles, {up_rate*100:.1f}% closed up historically, "
            f"test AUC {auc_str}, accuracy {accuracy*100:.1f}%")


def train() -> str:
    """Trains every symbol in config.PREDICT_SYMBOLS, returns a combined summary."""
    results = [train_symbol(symbol) for symbol in config.PREDICT_SYMBOLS]
    return "Price-trend models:\n  " + "\n  ".join(results) + "\n\nAvailable via /predict in Telegram."


if __name__ == "__main__":
    print(train())
