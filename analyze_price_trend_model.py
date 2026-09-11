"""
Trains an XGBoost classifier per coin (config.PREDICT_SYMBOLS) predicting
whether price will be higher HORIZON_CANDLES (60 one-minute candles, i.e. an
hour) from now than it is now, using market_candles history. Originally
predicted just the next single 1-minute candle, which didn't match how
prediction_tracker.py's paper-trading signal actually gets checked/acted on
(config.PREDICTION_CHECK_INTERVAL_MINUTES) - raised to an hour on 2026-09-08
so the probability and the decision cadence mean the same thing.

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

Hyperparameters (max_depth/n_estimators/learning_rate) are tuned per
symbol per run via GridSearchCV over PARAM_GRID, scored with
PurgedTimeSeriesSplit - walk-forward CV, not a random k-fold shuffle, since
a random shuffle would let a fold "train" on rows chronologically after
what it's "testing" on (the same leakage concern that keeps the final
train/test split below unshuffled). Measured at ~6.6s/symbol at full
90-day/~90k-row scale, so ~1 minute added across all of
config.PREDICT_SYMBOLS - nowhere near the 57-minute unbounded-backfill hang
from 2026-09-09 (a different, since-fixed issue in ensure_backfilled below).

--- Purging and honest uncertainty (2026-09-11) ---

Two related problems, found by actually checking the numbers this pipeline
had been reporting: every symbol's test AUC was sitting at ~0.48-0.54,
indistinguishable from a coin flip, and CV AUC was sometimes *below* 0.5.

1. Leakage at every train/test boundary. A row's label looks
   HORIZON_CANDLES (60) minutes into the future. The last 60 rows of any
   chronological "train" slice therefore have labels computed from prices
   that fall inside the following "test" slice - a real leak, not just a
   statistics problem. PurgedTimeSeriesSplit below wraps sklearn's
   TimeSeriesSplit and additionally drops the last `purge` rows of each
   fold's train indices; train_symbol() does the equivalent by hand at the
   outer train/test split.

2. The bigger problem: even leak-free, adjacent rows' labels overlap in
   59 of their 60 minutes (row t and row t+1 are both asking "is price up
   ~60min from now", almost the same question). They are not independent
   samples, so the usual AUC standard error formula
   (SE ~ sqrt((n+ + n- + 1) / (12 n+ n-)), which assumes i.i.d. test
   points) drastically understates the true uncertainty - by roughly
   sqrt(HORIZON_CANDLES) ~= 7.75x once you account for the ~60-row
   autocorrelation. A reported "AUC 0.53" was well within noise of 0.50;
   none of the 7 coins had shown a statistically real edge.

   Fixed by computing a moving-block bootstrap CI (block_bootstrap_auc_ci
   below): resample the test set in contiguous blocks of HORIZON_CANDLES
   rows (not individual rows) with replacement, recompute AUC per
   resample, take the 2.5th/97.5th percentiles. This correctly reflects
   the test set's real (much smaller) effective sample size without
   throwing away any data for the point estimate itself - AUC is still
   computed on the full test set, just with an honest error bar around it.

Every trained model's metadata now carries auc_ci_low/auc_ci_high, and the
training summary/predict text only ever calls a coin's edge "real" once
its whole CI clears 0.5.
"""

from datetime import datetime, timedelta, timezone
import pickle
import numpy as np
import pandas as pd
import psycopg2
from sklearn.model_selection import TimeSeriesSplit, GridSearchCV
from sklearn.metrics import roc_auc_score, accuracy_score
import xgboost as xgb
import config
import logger
from backfill_candles import fetch_binance_klines, BACKFILL_DAYS

MIN_ROWS = 500
# Number of moving-block bootstrap resamples for the AUC confidence
# interval - see block_bootstrap_auc_ci. 500 gives stable 2.5th/97.5th
# percentiles without materially adding to the per-symbol training budget
# (each resample is just a roc_auc_score call on ~13.5k already-computed
# values, not a refit).
BOOTSTRAP_ITERATIONS = 500
# Hyperparameter search grid for train_symbol()'s GridSearchCV - kept
# deliberately small (3 x 2 x 2 = 12 combos x 3 CV folds = 36 fits per
# symbol, ~252 total across config.PREDICT_SYMBOLS) so the daily cron run
# stays bounded - see ensure_backfilled's docstring for what an unbounded
# per-run cost already did once (a 57-minute hang). Widen only after
# confirming real runtime stays reasonable.
PARAM_GRID = {
    "max_depth": [3, 4, 5],
    "n_estimators": [100, 200],
    "learning_rate": [0.05, 0.1],
}
# How many 1-minute candles ahead the target looks - see module docstring.
# Keep in sync with config.PREDICTION_CHECK_INTERVAL_MINUTES (60): the
# probability price_predictor reports should describe the same span of time
# prediction_tracker.py actually waits between decisions.
HORIZON_CANDLES = 60
# Cap on how many days of volume-only backfill ensure_backfilled patches in
# per training run - see its docstring for why this exists (an unbounded
# fetch is what caused a run to hang without completing).
VOLUME_BACKFILL_CHUNK_DAYS = 15

# Shared with price_predictor.py's inference path - keep both in sync if
# either changes, they must compute features identically.
# volume_ratio: current-minute volume vs its own trailing 15-minute average -
# a busy vs quiet read, scale-invariant so it's comparable across coins with
# wildly different raw volume (BTC vs DOGE). btc_return_5: BTCUSDT's own
# 5-minute return at the same timestamp - BTC often moves first and alts
# follow a few minutes later, so this gives every other coin's model a look
# at what BTC just did (harmless near-duplicate of its own return_5 for the
# BTCUSDT model itself).
FEATURE_COLS = ["return_1", "return_5", "return_15", "volatility_15", "hour_sin", "hour_cos", "day_of_week",
                "volume_ratio", "btc_return_5"]


def model_name(symbol: str) -> str:
    return f"price_trend_{symbol.lower()}"


class PurgedTimeSeriesSplit:
    """Same expanding-window folds as sklearn's TimeSeriesSplit, but drops
    the last `purge` rows of each fold's train indices - those rows' labels
    look `purge` steps into the future and would otherwise peek into that
    fold's own test period. See module docstring for why this matters with
    HORIZON_CANDLES-ahead labels. Duck-types sklearn's CV splitter
    interface (get_n_splits/split) so GridSearchCV accepts it as `cv=`."""

    def __init__(self, n_splits: int = 3, purge: int = 0):
        self.n_splits = n_splits
        self.purge = purge
        self._base = TimeSeriesSplit(n_splits=n_splits)

    def get_n_splits(self, X=None, y=None, groups=None):
        return self.n_splits

    def split(self, X, y=None, groups=None):
        for train_idx, test_idx in self._base.split(X):
            if self.purge > 0:
                train_idx = train_idx[:-self.purge] if len(train_idx) > self.purge else train_idx[:0]
            yield train_idx, test_idx


def block_bootstrap_auc_ci(y_test, y_prob, block_size: int, n_iterations: int = BOOTSTRAP_ITERATIONS,
                            ci: float = 0.95, seed: int = 42):
    """95% CI for AUC via moving-block bootstrap, honest about the
    ~block_size-row autocorrelation HORIZON_CANDLES-ahead labels create
    (see module docstring) - resamples contiguous blocks of `block_size`
    rows with replacement instead of individual rows, recomputing AUC each
    time. Cheap: pure resampling of already-computed y_test/y_prob pairs,
    no retraining. Returns (ci_low, ci_high), or (None, None) if too many
    resamples came back degenerate (all-one-class) to trust the result -
    can happen on a small/imbalanced test set.
    """
    y_test = np.asarray(y_test)
    y_prob = np.asarray(y_prob)
    n = len(y_test)
    n_blocks = int(np.ceil(n / block_size))
    rng = np.random.default_rng(seed)

    aucs = []
    for _ in range(n_iterations):
        starts = rng.integers(0, max(1, n - block_size + 1), size=n_blocks)
        idx = np.concatenate([np.arange(s, min(s + block_size, n)) for s in starts])[:n]
        y_s, p_s = y_test[idx], y_prob[idx]
        if len(np.unique(y_s)) < 2:
            continue  # degenerate resample (one class only) - skip rather than let roc_auc_score raise
        aucs.append(roc_auc_score(y_s, p_s))

    if len(aucs) < n_iterations * 0.5:
        return None, None
    lo, hi = np.percentile(aucs, [(1 - ci) / 2 * 100, (1 + ci) / 2 * 100])
    return float(lo), float(hi)


def load_candles(symbol: str) -> pd.DataFrame:
    conn = psycopg2.connect(config.DATABASE_URL)
    df = pd.read_sql(
        "SELECT candle_start, close, volume FROM market_candles WHERE symbol = %(symbol)s ORDER BY candle_start",
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
    use ON CONFLICT DO NOTHING for OHLC (see logger.bulk_log_candles), so
    re-running this against a symbol that's already fully backfilled just
    fetches an empty/tiny gap and no-ops there.

    Also patches in volume for rows that predate that column, in bounded
    VOLUME_BACKFILL_CHUNK_DAYS-sized chunks starting from the oldest still-
    missing row - not the whole BACKFILL_DAYS window in one fetch. A single
    all-at-once fetch (7 coins x 90 days each) is what caused a training run
    to hang for 57+ minutes and never complete on 2026-09-09 - bounding it
    means each run makes bounded, safe progress and the backlog clears over
    several days instead of risking the whole run every time.
    """
    end = datetime.now(timezone.utc)
    desired_start = end - timedelta(days=BACKFILL_DAYS)
    earliest = df["candle_start"].min() if len(df) else None
    covers_window = earliest is not None and earliest <= desired_start + timedelta(hours=1)

    if not covers_window:
        fetch_end = earliest if earliest is not None else end
        rows = fetch_binance_klines(symbol, int(desired_start.timestamp() * 1000), int(fetch_end.timestamp() * 1000))
        logger.bulk_log_candles(rows)
        return

    missing_volume_start = df.loc[df["volume"].isna(), "candle_start"].min() if len(df) else None
    # .min() on an empty selection (no NaN rows) returns NaT, not None -
    # pd.isna() catches both that and a genuine None from the empty-df case.
    if pd.isna(missing_volume_start):
        return  # already covers the full desired window, with volume throughout

    fetch_end = min(end, missing_volume_start + timedelta(days=VOLUME_BACKFILL_CHUNK_DAYS))
    rows = fetch_binance_klines(symbol, int(missing_volume_start.timestamp() * 1000), int(fetch_end.timestamp() * 1000))
    logger.bulk_log_candles(rows)


def load_btc_lag_series() -> pd.DataFrame:
    """BTCUSDT's own 5-minute return, indexed by candle_start - merged into
    every coin's features as btc_return_5 (see FEATURE_COLS)."""
    btc = load_candles("BTCUSDT")
    btc["btc_return_5"] = btc["close"].pct_change(5)
    return btc[["candle_start", "btc_return_5"]]


def build_features(df: pd.DataFrame, btc_lag: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["return_1"] = df["close"].pct_change(1)
    df["return_5"] = df["close"].pct_change(5)
    df["return_15"] = df["close"].pct_change(15)
    df["volatility_15"] = df["return_1"].rolling(15).std()
    df["hour_sin"] = np.sin(2 * np.pi * df["candle_start"].dt.hour / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df["candle_start"].dt.hour / 24)
    df["day_of_week"] = df["candle_start"].dt.dayofweek
    df["volume_ma_15"] = df["volume"].rolling(15).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma_15"]
    # merge_asof (nearest BTC row at or before this one), not an exact
    # candle_start match - backfilled rows for every symbol land on
    # Binance's own clean per-minute grid so an exact merge looked fine in
    # training, but live-collected candles are built independently per
    # symbol (each one's own candle boundary is whenever its first tick
    # after the last flush happened to arrive, not a shared wall-clock
    # grid) and essentially never land on the same timestamp as BTCUSDT's -
    # that left btc_return_5 NaN for every live prediction except BTCUSDT's
    # own. tolerance bounds how stale a match can be if there's a gap.
    df = df.sort_values("candle_start")
    df = pd.merge_asof(df, btc_lag.sort_values("candle_start"), on="candle_start",
                        direction="backward", tolerance=pd.Timedelta(minutes=5))
    # target: is price higher HORIZON_CANDLES ahead than it is now? NaN (not
    # False) for the last HORIZON_CANDLES rows, which have no future price to
    # compare against - "shift(...) > x" silently evaluates a NaN comparison
    # as False rather than NaN, so np.where makes that explicit here instead,
    # to be dropped below rather than mislabeled "down". With HORIZON_CANDLES
    # at 60 (vs the original 1) this now affects 60 rows per symbol instead
    # of 1 - trivial against ~130k rows of backfilled history, but wrong is
    # wrong.
    future_close = df["close"].shift(-HORIZON_CANDLES)
    df["target"] = np.where(future_close.notna(), future_close > df["close"], np.nan)
    df = df.dropna().reset_index(drop=True)
    df["target"] = df["target"].astype(int)
    return df


def train_symbol(symbol: str) -> str:
    df = load_candles(symbol)
    ensure_backfilled(symbol, df)
    df = load_candles(symbol)  # re-read - picks up whatever ensure_backfilled just topped up, no-op otherwise

    if len(df) < MIN_ROWS:
        return f"{symbol}: only {len(df)} candles so far (need {MIN_ROWS}+) - too early to train."

    df = build_features(df, load_btc_lag_series())
    X, y = df[FEATURE_COLS], df["target"]

    # Chronological 80/20 split, purged at the boundary: the last
    # HORIZON_CANDLES rows before the test period start have labels that
    # look HORIZON_CANDLES minutes into the future - i.e. into the test
    # period itself (see module docstring's "purging" section) - so they're
    # dropped entirely rather than assigned to either side.
    n = len(df)
    test_start = n - int(n * 0.2)
    train_end = max(0, test_start - HORIZON_CANDLES)
    X_train, y_train = X.iloc[:train_end], y.iloc[:train_end]
    X_test, y_test = X.iloc[test_start:], y.iloc[test_start:]

    # Hyperparameter search over PARAM_GRID, scored by walk-forward CV
    # (PurgedTimeSeriesSplit) rather than plain k-fold - a random k-fold
    # shuffle would let a fold "train" on rows chronologically after what
    # it's "testing" on, the exact leakage the purged split above already
    # guards against, just generalized to multiple internal fold
    # boundaries too. GridSearchCV refits one final model on the full
    # X_train/y_train using whichever combination scored best across folds
    # (roc_auc, matching how we report performance below).
    search = GridSearchCV(
        xgb.XGBClassifier(eval_metric="logloss"),
        PARAM_GRID,
        cv=PurgedTimeSeriesSplit(n_splits=3, purge=HORIZON_CANDLES),
        scoring="roc_auc",
        refit=True,
    )
    search.fit(X_train, y_train)
    model = search.best_estimator_

    # Per-fold AUC for the winning params - GridSearchCV already computes
    # this internally (cv_results_) to pick best_params_, but only the mean
    # (best_score_) got kept before. Individually, these are 3 chronological
    # chunks of the training window (earliest -> latest), so a wide spread
    # between them is a direct sign of regime drift - the market behaving
    # differently early vs late in the window - rather than one stable
    # pattern the mean quietly averages over. float(): same numpy-float64
    # JSON-serialization issue as best_score_ below.
    fold_aucs = [float(search.cv_results_[f"split{i}_test_score"][search.best_index_])
                 for i in range(search.n_splits_)]
    fold_spread = max(fold_aucs) - min(fold_aucs)

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

    # Point estimate above uses the full test set (best precision); the CI
    # accounts for the ~HORIZON_CANDLES-row autocorrelation a plain i.i.d.
    # standard-error formula would miss entirely - see module docstring and
    # block_bootstrap_auc_ci. A coin only counts as having a demonstrated
    # edge once the WHOLE interval clears 0.5, not just the point estimate.
    ci_low = ci_high = None
    significant = False
    ci_str = ""
    if auc is not None:
        ci_low, ci_high = block_bootstrap_auc_ci(y_test, y_prob, block_size=HORIZON_CANDLES)
        if ci_low is not None:
            significant = ci_low > 0.5
            # 3dp, not 2 - a boundary case like ci_low=0.501 would otherwise
            # round to "0.50" and look identical to a genuinely non-significant
            # result (ci_low=0.499 also rounds to "0.50").
            ci_str = f" [{ci_low:.3f}, {ci_high:.3f}]" + ("" if significant else " (not distinguishable from chance)")
        else:
            ci_str = " (CI unavailable - too many degenerate resamples)"

    # search.best_score_ is a numpy float64 - json.dumps (used by
    # logger.save_model's Json() wrapper) can't serialize that, same class
    # of bug as the NaN-vs-None issue elsewhere in this file. Cast to plain
    # Python float before it ever reaches Postgres.
    blob = pickle.dumps(model)
    logger.save_model(model_name(symbol), blob, {
        "auc": auc, "auc_ci_low": ci_low, "auc_ci_high": ci_high, "auc_significant": significant,
        "accuracy": accuracy, "rows": len(df), "features": FEATURE_COLS,
        "best_params": search.best_params_, "cv_auc": round(float(search.best_score_), 3),
        "cv_fold_aucs": [round(a, 3) for a in fold_aucs], "cv_fold_spread": round(fold_spread, 3),
    })

    up_rate = df["target"].mean()
    folds_str = ", ".join(f"{a:.3f}" for a in fold_aucs)
    return (f"{symbol}: {len(df)} candles, {up_rate*100:.1f}% were higher an hour later historically, "
            f"test AUC {auc_str}{ci_str}, accuracy {accuracy*100:.1f}%, "
            f"best params {search.best_params_} "
            f"(CV AUC {search.best_score_:.3f}, folds [{folds_str}] spread {fold_spread:.3f})")


def train() -> str:
    """Trains every symbol in config.PREDICT_SYMBOLS, returns a combined
    summary. Isolated per-symbol - one coin hitting a network hiccup or
    slow fetch shouldn't take the whole run down and leave every other
    coin (and the run's own recorded result) stuck on stale data too."""
    results = []
    for symbol in config.PREDICT_SYMBOLS:
        try:
            results.append(train_symbol(symbol))
        except Exception as e:
            results.append(f"{symbol}: training failed - {e}")
    return "Price-trend models:\n  " + "\n  ".join(results) + "\n\nAvailable via /predict in Telegram."


if __name__ == "__main__":
    print(train())
