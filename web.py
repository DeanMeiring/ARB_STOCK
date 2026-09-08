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

_DASHBOARD_HTML = (pathlib.Path(__file__).parent / "web" / "dashboard.html").read_text()


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


@app.get("/api/stats")
def api_stats(_auth=Depends(require_auth)):
    return logger.get_stats_json()


@app.get("/api/opportunities")
def api_opportunities(hours: int = 24, source: str = None, _auth=Depends(require_auth)):
    return logger.get_recent_opportunities(hours, source)


@app.get("/api/near_misses")
def api_near_misses(hours: int = 24, source: str = None, _auth=Depends(require_auth)):
    return logger.get_recent_near_misses(hours, source)


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


@app.get("/api/threshold_crossings")
def api_threshold_crossings(hours: int = 4, _auth=Depends(require_auth)):
    """How many times a coin crossed >= config.PREDICT_UP_THRESHOLD in the
    last `hours` - the dashboard's "Above 65% (last 4h)" tile."""
    return logger.get_threshold_crossings(hours)


@app.get("/api/paper_trades")
def api_paper_trades(symbol: str = "BTCUSDT", hours: int = 24, _auth=Depends(require_auth)):
    """Every paper trade (open or closed) for one coin in the selected
    window - feeds the price chart's buy/sell markers and its trades list."""
    return logger.get_recent_paper_trades(symbol, hours)


@app.get("/api/prediction_accuracy")
def api_prediction_accuracy(_auth=Depends(require_auth)):
    """Today's (SAST) tally of paper_trades closed by prediction_tracker.py's
    threshold-crossing signal - see logger.get_todays_paper_trade_stats."""
    return logger.get_todays_paper_trade_stats()


@app.get("/api/predictions")
def api_predictions(_auth=Depends(require_auth)):
    """Current price-trend prediction per coin - pandas/xgboost only load
    when this endpoint is actually hit, same deferred-import pattern as the
    Telegram /predict handler."""
    import price_predictor
    results = price_predictor.predict_all()
    return [
        {
            "symbol": r["symbol"], "prob_up": r["prob_up"], "auc": r["auc"],
            "accuracy": r["accuracy"], "trained_at": r["trained_at"].isoformat(),
        }
        for r in results
    ]
