"""
Live dashboard - a small FastAPI app serving one HTML page (web/dashboard.html)
with Chart.js graphs, fed by JSON endpoints that read straight from the
Postgres tables logger.py already writes to (opportunities, near_misses,
market_candles, trained_models, training_runs). Covers both detectors -
triangular (Binance-only) and cross-exchange (Binance vs Crypto.com).

Runs as another asyncio task alongside the price stream, cross-exchange
poller, Telegram poller, and watchdog (see main.py) - same event loop, no
extra process, no extra stored data of its own.

Protected by HTTP Basic Auth when config.DASHBOARD_PASSWORD is set - strongly
recommended once deployed, since Railway will expose this on a public URL.
"""

import pathlib
import secrets

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import config
import logger

app = FastAPI(title="ARB_STOCK Dashboard")
security = HTTPBasic(auto_error=False)

# Set by main.py's prediction_tracking_loop at the start of each sleep cycle -
# in-process shared state, not Postgres, since it's purely "when will this
# same running process next wake up and check" and has no meaning beyond
# that process's own lifetime (a redeploy resets it, same as the check
# timer itself does).
prediction_schedule = {"next_check_at": None}

# Set by prediction_tracker.check_signals() each time it actually runs (once
# an hour) - a frozen snapshot of exactly what the paper-trading loop saw
# and acted on, as opposed to /api/predictions' numbers which recompute
# live on every dashboard refresh. Same in-process-only reasoning as
# prediction_schedule above.
last_check_snapshot = {"checked_at": None, "predictions": []}

# Same as last_check_snapshot above, but for the cross-sectional (relative-
# strength) strategy - see analyze_price_trend_model.build_features_relative
# and prediction_tracker.check_signals(strategy="relative"). Kept as a
# separate dict rather than reusing last_check_snapshot with a strategy key
# so a crash/bug in one strategy's tracking can never clobber the other's
# last-known state.
last_check_snapshot_relative = {"checked_at": None, "predictions": []}

_DASHBOARD_HTML = (pathlib.Path(__file__).parent / "web" / "dashboard.html").read_text()
_RELATIVE_DASHBOARD_HTML = (pathlib.Path(__file__).parent / "web" / "relative_dashboard.html").read_text()


def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    if not config.DASHBOARD_PASSWORD:
        return  # no password configured - dashboard is open, local/dev use only

    valid = (
        credentials is not None
        and secrets.compare_digest(credentials.username, "admin")
        and secrets.compare_digest(credentials.password, config.DASHBOARD_PASSWORD)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


@app.get("/", response_class=HTMLResponse)
def dashboard(_auth=Depends(require_auth)):
    return _DASHBOARD_HTML


@app.get("/relative", response_class=HTMLResponse)
def relative_dashboard(_auth=Depends(require_auth)):
    """Separate page for the cross-sectional (relative-strength) model - own
    predictions, own mock paper trades/P&L, plus a side-by-side AUC/CI
    comparison against the absolute model per coin. Deliberately not a tab
    on the main dashboard: this strategy hasn't been promoted to /predict
    yet and is still being evaluated on its own."""
    return _RELATIVE_DASHBOARD_HTML


@app.get("/api/stats")
def api_stats(_auth=Depends(require_auth)):
    return logger.get_stats_json()


@app.get("/api/opportunities")
def api_opportunities(hours: int = 24, source: str = None, _auth=Depends(require_auth)):
    return logger.get_recent_opportunities(hours, source)


@app.get("/api/near_misses")
def api_near_misses(hours: int = 24, source: str = None, _auth=Depends(require_auth)):
    return logger.get_recent_near_misses(hours, source)


@app.get("/api/arb_pnl")
def api_arb_pnl(_auth=Depends(require_auth)):
    """Hypothetical $ P&L per arbitrage detector - see get_arb_pnl_stats'
    docstring for the idealized-execution caveat."""
    return {
        "triangular": logger.get_arb_pnl_stats("triangular"),
        "cross_exchange": logger.get_arb_pnl_stats("cross_exchange"),
    }


@app.get("/api/candles")
def api_candles(symbol: str = "BTCUSDT", hours: int = 24, _auth=Depends(require_auth)):
    return logger.get_recent_candles(symbol, hours)


@app.get("/api/candles_multi")
def api_candles_multi(hours: int = 24, _auth=Depends(require_auth)):
    """Every config.PREDICT_SYMBOLS coin's candles in one call - feeds the
    dashboard's multi-coin signal widget without a round trip per coin."""
    return logger.get_recent_candles_multi(config.PREDICT_SYMBOLS, hours)


@app.get("/api/models")
def api_models(_auth=Depends(require_auth)):
    return logger.get_trained_models_json()


@app.get("/api/config")
def api_config(_auth=Depends(require_auth)):
    """Static thresholds the frontend needs to draw reference lines etc."""
    return {
        "min_profit_threshold_pct": config.MIN_PROFIT_THRESHOLD * 100,
        "predict_up_threshold_pct": config.PREDICT_UP_THRESHOLD * 100,
        "predict_symbols": config.PREDICT_SYMBOLS,
    }


@app.get("/api/prediction_schedule")
def api_prediction_schedule(_auth=Depends(require_auth)):
    """When prediction_tracking_loop will next wake up and check every
    coin's signal - see the prediction_schedule module-level dict above."""
    next_at = prediction_schedule["next_check_at"]
    return {"next_check_at": next_at.isoformat() if next_at else None}


@app.get("/api/last_check_snapshot")
def api_last_check_snapshot(strategy: str = "absolute", _auth=Depends(require_auth)):
    """The frozen prob_up per coin as of the last actual hourly check - see
    the last_check_snapshot/last_check_snapshot_relative module-level dicts
    above."""
    snapshot = last_check_snapshot if strategy == "absolute" else last_check_snapshot_relative
    checked_at = snapshot["checked_at"]
    return {
        "checked_at": checked_at.isoformat() if checked_at else None,
        "predictions": snapshot["predictions"],
    }


@app.get("/api/prediction_vs_actual")
def api_prediction_vs_actual(hours: int = 48, strategy: str = "absolute", _auth=Depends(require_auth)):
    """Every symbol's price-at-check vs. the actual price ~1h later, for
    every real hourly check in the window whose hour has elapsed - see
    logger.get_prediction_vs_actual."""
    return logger.get_prediction_vs_actual(hours, strategy)


@app.get("/api/threshold_crossings")
def api_threshold_crossings(hours: int = 4, strategy: str = "absolute", _auth=Depends(require_auth)):
    """How many times a coin crossed >= config.PREDICT_UP_THRESHOLD in the
    last `hours` - the dashboard's "Above 65% (last 4h)" tile."""
    return logger.get_threshold_crossings(hours, strategy)


@app.get("/api/paper_trades")
def api_paper_trades(symbol: str = "BTCUSDT", hours: int = 24, strategy: str = "absolute",
                      _auth=Depends(require_auth)):
    """Every paper trade (open or closed) for one coin in the selected
    window - feeds the price chart's buy/sell markers and its trades list."""
    return logger.get_recent_paper_trades(symbol, hours, strategy)


@app.get("/api/prediction_accuracy")
def api_prediction_accuracy(strategy: str = "absolute", _auth=Depends(require_auth)):
    """Today's (SAST) tally of paper_trades closed by prediction_tracker.py's
    threshold-crossing signal - see logger.get_todays_paper_trade_stats."""
    return logger.get_todays_paper_trade_stats(strategy)


@app.get("/api/predictions")
def api_predictions(strategy: str = "absolute", _auth=Depends(require_auth)):
    """Current price-trend prediction per coin - pandas/xgboost only load
    when this endpoint is actually hit, same deferred-import pattern as the
    Telegram /predict handler."""
    import price_predictor
    results = price_predictor.predict_all() if strategy == "absolute" else price_predictor.predict_all_relative()
    return [
        {
            "symbol": r["symbol"], "prob_up": r["prob_up"], "auc": r["auc"],
            "accuracy": r["accuracy"], "trained_at": r["trained_at"].isoformat(),
            "auc_ci_low": r.get("auc_ci_low"), "auc_ci_high": r.get("auc_ci_high"),
            "auc_significant": r.get("auc_significant", False),
        }
        for r in results
    ]


@app.get("/api/predictions_compare")
def api_predictions_compare(_auth=Depends(require_auth)):
    """Side-by-side absolute vs. relative model stats per coin, for the
    /relative page's comparison table - lets you see directly whether the
    cross-sectional reframing actually found a more trustworthy edge than
    the plain up/down model, coin by coin, without cross-referencing two
    separate /api/predictions calls by hand."""
    import price_predictor
    absolute = {r["symbol"]: r for r in price_predictor.predict_all()}
    relative = {r["symbol"]: r for r in price_predictor.predict_all_relative()}
    rows = []
    for symbol in config.PREDICT_SYMBOLS:
        a, r = absolute.get(symbol), relative.get(symbol)
        rows.append({
            "symbol": symbol,
            "absolute": None if a is None else {
                "auc": a["auc"], "auc_ci_low": a.get("auc_ci_low"), "auc_ci_high": a.get("auc_ci_high"),
                "auc_significant": a.get("auc_significant", False), "prob_up": a["prob_up"],
            },
            "relative": None if r is None else {
                "auc": r["auc"], "auc_ci_low": r.get("auc_ci_low"), "auc_ci_high": r.get("auc_ci_high"),
                "auc_significant": r.get("auc_significant", False), "prob_up": r["prob_up"],
            },
        })
    return rows
