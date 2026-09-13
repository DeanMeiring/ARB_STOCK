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
from analyze_price_trend_model import FEATURE_COLS, model_name, model_name_relative


def _load_latest_features(symbol: str):
    """Loads this symbol's most recent candles and computes the same
    technical features both target framings train on (see
    analyze_price_trend_model._add_technical_features) - shared by
    predict_symbol and predict_symbol_relative, which differ only in which
    trained model gets applied to this same feature row (the cross-sectional
    target only affects what TRAINING labels as positive, not what a live
    prediction needs as input - see build_features_relative's docstring).

    Returns (latest_row_df, price) on success, or a plain string explaining
    why there's nothing to predict from yet.
    """
    conn = psycopg2.connect(config.DATABASE_URL)
    df = pd.read_sql(
        "SELECT candle_start, close, volume FROM market_candles WHERE symbol = %(symbol)s "
        "ORDER BY candle_start DESC LIMIT 30",
        conn, params={"symbol": symbol}, parse_dates=["candle_start"],
    )
    # Same btc_return_5 feature as training (see analyze_price_trend_model.
    # load_btc_lag_series) - fetched here too since inference needs it live,
    # not just at training time. Harmless extra query when symbol is itself
    # BTCUSDT (re-reads the same data as df above).
    btc_df = pd.read_sql(
        "SELECT candle_start, close FROM market_candles WHERE symbol = 'BTCUSDT' "
        "ORDER BY candle_start DESC LIMIT 30",
        conn, parse_dates=["candle_start"],
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
    df["volume_ma_15"] = df["volume"].rolling(15).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma_15"]

    btc_df["candle_start"] = pd.to_datetime(btc_df["candle_start"], utc=True)
    btc_df = btc_df.sort_values("candle_start").reset_index(drop=True)
    btc_df["btc_return_5"] = btc_df["close"].pct_change(5)
    # merge_asof (nearest BTC row at or before this one), not an exact
    # candle_start match - see analyze_price_trend_model.build_features for
    # why: live candles for different symbols essentially never land on the
    # same timestamp as BTCUSDT's own, which left this NaN for every coin
    # but BTCUSDT itself.
    df = pd.merge_asof(df, btc_df[["candle_start", "btc_return_5"]], on="candle_start",
                        direction="backward", tolerance=pd.Timedelta(minutes=5))

    latest = df.iloc[[-1]][FEATURE_COLS]
    if latest.isnull().any(axis=1).iloc[0]:
        return f"{symbol}: not enough recent history to compute features yet"
    return latest, float(df.iloc[-1]["close"])


def _predict_with_model(symbol: str, saved_model_name: str):
    """Loads saved_model_name and applies it to symbol's latest feature row.
    Returns a dict {symbol, prob_up, trained_at, auc, accuracy, auc_ci_low,
    auc_ci_high, auc_significant, price} on success, or a plain string
    explaining why there's no prediction right now. prob_up means "model's
    predicted probability of its positive class" generically - what that
    class actually IS (price went up vs. beat the basket) depends on which
    model_name was passed in; predict_symbol/predict_symbol_relative below
    are just this applied to the two different saved models."""
    saved = logger.load_model(saved_model_name)
    if not saved:
        return f"{symbol}: no trained model yet"
    blob, metadata, trained_at = saved
    model = pickle.loads(blob)

    # A saved model is trained on whatever FEATURE_COLS was at the time -
    # if that's changed since (e.g. this deploy added volume_ratio/
    # btc_return_5) and this symbol hasn't been retrained yet, XGBoost
    # raises rather than degrading, so catch the mismatch explicitly
    # instead of letting predict_proba below blow up every single call
    # until the next training run catches up.
    trained_features = model.get_booster().feature_names
    if trained_features is not None and list(trained_features) != FEATURE_COLS:
        return f"{symbol}: model needs retraining (feature set changed) - waiting on next training run"

    result = _load_latest_features(symbol)
    if isinstance(result, str):
        return result
    latest, price = result

    prob_up = float(model.predict_proba(latest)[0][1])
    metadata = metadata or {}
    return {
        "symbol": symbol, "prob_up": prob_up, "trained_at": trained_at,
        "auc": metadata.get("auc"), "accuracy": metadata.get("accuracy"),
        "auc_ci_low": metadata.get("auc_ci_low"), "auc_ci_high": metadata.get("auc_ci_high"),
        "auc_significant": metadata.get("auc_significant", False),
        "price": price,
    }


def predict_symbol(symbol: str):
    """Absolute-direction prediction: probability price is higher an hour
    from now. See _predict_with_model."""
    return _predict_with_model(symbol, model_name(symbol))


def predict_symbol_relative(symbol: str):
    """Cross-sectional prediction: probability this coin beats the basket's
    mean return an hour from now (see analyze_price_trend_model.
    build_features_relative). Same dict shape as predict_symbol - prob_up
    here means "probability of outperforming the basket", not absolute
    direction."""
    return _predict_with_model(symbol, model_name_relative(symbol))


def predict_all() -> list:
    """[{symbol, prob_up, trained_at, auc, accuracy}, ...] for every symbol with a usable model, sorted by prob_up desc."""
    results = [predict_symbol(s) for s in config.PREDICT_SYMBOLS]
    ok = [r for r in results if isinstance(r, dict)]
    ok.sort(key=lambda r: r["prob_up"], reverse=True)
    return ok


def predict_all_relative() -> list:
    """Cross-sectional counterpart of predict_all - feeds the /relative
    dashboard page's predictions list."""
    results = [predict_symbol_relative(s) for s in config.PREDICT_SYMBOLS]
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
        accuracy_str = f", accuracy {r['accuracy']*100:.0f}%" if r.get("accuracy") is not None else ""
        # A coin only gets called out as having a real edge once its whole
        # bootstrap CI clears 0.5 - see analyze_price_trend_model's module
        # docstring on why the bare AUC point estimate isn't trustworthy on
        # its own (~60-row autocorrelation from the 1h-ahead label).
        if r.get("auc_significant"):
            # 3dp - see analyze_price_trend_model's ci_str comment: 2dp can
            # make a genuine boundary case (ci_low=0.501) round to "0.50"
            # and look identical to a non-significant one.
            edge_str = f", real edge: AUC {r['auc']:.3f} [{r['auc_ci_low']:.3f}, {r['auc_ci_high']:.3f}]"
        else:
            edge_str = " (no proven edge yet)"
        lines.append(f"  {r['symbol']}: {r['prob_up']*100:.0f}%{marker}{accuracy_str}{edge_str}")

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
