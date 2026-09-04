"""
Trains an XGBoost classifier predicting whether the NEXT sampled spread
(near-miss or opportunity) will clear the profit threshold, using the
history already sitting in Postgres.

Standalone - not part of main.py's always-on loop. Meant to be run
periodically (a Railway cron-configured service, see the deploy notes in
the repo), not continuously: each run pulls the latest data, retrains
from scratch (data this small doesn't warrant incremental training), and
posts a summary via Telegram so results are visible without digging
through logs.

Honest caveat, worth repeating every run: this only has as much signal as
the bot has accumulated. Early runs will have very little history and
correspondingly weak/unreliable results - that's expected, not a bug.
Model quality should improve as more weeks of data accumulate.
"""

import sys
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import psycopg2
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, roc_auc_score
import xgboost as xgb
import config
import logger

MIN_ROWS = 50  # below this, there's not enough history to say anything
LOOKAHEAD = 1  # predict whether the very next sample clears threshold


def load_data(source: str) -> pd.DataFrame:
    conn = psycopg2.connect(config.DATABASE_URL)
    # parse_dates: pd.read_sql doesn't reliably infer TIMESTAMPTZ as
    # datetime64 over a raw psycopg2 connection (vs. SQLAlchemy), which
    # silently breaks every .dt accessor used in build_features below.
    near_misses = pd.read_sql(
        "SELECT timestamp, profit_pct FROM near_misses WHERE source = %(source)s",
        conn, params={"source": source}, parse_dates=["timestamp"],
    )
    opportunities = pd.read_sql(
        "SELECT timestamp, profit_pct FROM opportunities WHERE source = %(source)s",
        conn, params={"source": source}, parse_dates=["timestamp"],
    )
    conn.close()

    # An empty table infers a different (tz-naive) dtype than a populated one
    # (tz-aware) - very likely early on, when one of the two tables has zero
    # rows for a source. Concatenating the two as-is silently falls back to
    # object dtype, which then breaks every .dt accessor downstream. Coerce
    # both explicitly so they're always consistent regardless of emptiness.
    near_misses["timestamp"] = pd.to_datetime(near_misses["timestamp"], utc=True)
    opportunities["timestamp"] = pd.to_datetime(opportunities["timestamp"], utc=True)

    df = pd.concat([near_misses, opportunities], ignore_index=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["hour_sin"] = np.sin(2 * np.pi * df["timestamp"].dt.hour / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df["timestamp"].dt.hour / 24)
    df["day_of_week"] = df["timestamp"].dt.dayofweek
    df["rolling_mean_5"] = df["profit_pct"].rolling(5, min_periods=1).mean()
    df["rolling_std_5"] = df["profit_pct"].rolling(5, min_periods=1).std().fillna(0)
    # target: does the NEXT sample clear threshold? (shift(-1) looks forward,
    # so this is only known in hindsight - exactly what we're trying to predict)
    df["target"] = (df["profit_pct"].shift(-LOOKAHEAD) > config.MIN_PROFIT_THRESHOLD).astype(int)
    df = df.iloc[:-LOOKAHEAD]  # last row(s) have no future sample to label
    return df


def train_and_report(source: str, label: str) -> str:
    df = load_data(source)
    if len(df) < MIN_ROWS:
        return f"{label}: only {len(df)} samples so far (need {MIN_ROWS}+) - too early to train."

    df = build_features(df)
    positive_rate = df["target"].mean()
    if df["target"].nunique() < 2:
        return (f"{label}: {len(df)} samples, but {'0' if positive_rate == 0 else '100'}% "
                f"positive rate - no variation to learn from yet.")

    feature_cols = ["profit_pct", "hour_sin", "hour_cos", "day_of_week", "rolling_mean_5", "rolling_std_5"]
    X = df[feature_cols]
    y = df["target"]
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, shuffle=False)  # shuffle=False: keep time order, no future leaking into train

    model = xgb.XGBClassifier(n_estimators=100, max_depth=3, eval_metric="logloss")
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]
    try:
        auc = roc_auc_score(y_test, y_prob)
        auc_str = f"{auc:.3f}"
    except ValueError:
        auc_str = "n/a (test set has no positive examples)"

    importances = sorted(zip(feature_cols, model.feature_importances_), key=lambda x: -x[1])
    top_features = ", ".join(f"{name} ({imp:.2f})" for name, imp in importances[:3])

    return (
        f"{label}: {len(df)} samples, {positive_rate*100:.1f}% cleared threshold historically.\n"
        f"  Test AUC: {auc_str} (0.5 = no better than chance, 1.0 = perfect)\n"
        f"  Top features: {top_features}"
    )


def main():
    print("Training opportunity-likelihood models...")
    results = [
        train_and_report("triangular", "Triangular (Binance)"),
        train_and_report("cross_exchange", "Cross-Exchange (Binance vs Crypto.com)"),
    ]

    print("Training price-trend model...")
    import analyze_price_trend_model
    results.append(analyze_price_trend_model.train())

    summary = "🤖 Model Training Report\n\n" + "\n\n".join(results)
    print(summary)

    subscribers = logger.get_subscribers()
    if not subscribers and config.TELEGRAM_API_BOT:
        print("No Telegram subscribers to notify.")
        return
    if config.TELEGRAM_API_BOT:
        import requests
        for chat_id in subscribers:
            requests.post(
                f"https://api.telegram.org/bot{config.TELEGRAM_API_BOT}/sendMessage",
                json={"chat_id": chat_id, "text": summary},
                timeout=10,
            )


if __name__ == "__main__":
    main()
