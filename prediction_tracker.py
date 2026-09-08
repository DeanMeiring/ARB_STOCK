"""
Tracks how price_predictor's calls actually play out, and simulates the
P&L of trading every one of them - purely on paper. No order is ever
placed here; this is entirely independent of executor.py/governor.py.

Every config.PREDICTION_LOG_INTERVAL_MINUTES, main.py's prediction_tracking_
loop() calls both functions below:

- log_due_predictions(): asks price_predictor for each config.PREDICT_SYMBOLS
  coin's current call and records it (logger.log_prediction) with the price
  at that moment and a resolve_at config.PREDICTION_HORIZON_MINUTES later -
  "the window it had to sell".
- resolve_due_predictions(): scores every prediction whose window has
  elapsed against the live price now (logger.get_latest_close - no model
  inference needed for this half), and computes what a hypothetical
  config.TRADE_SIZE_USDT position would have made/lost over that window,
  fees included both ways (config.TAKER_FEE, charged on entry and exit).

logger.get_todays_prediction_stats() is what /predict and the dashboard
read to show today's accuracy and hypothetical P&L.
"""

import config
import logger


def log_due_predictions():
    """Deferred import - same reasoning as telegram_bot.py's /predict
    handler: pandas/xgboost only load when a prediction is actually needed,
    never in the always-on detector's own import path."""
    import price_predictor

    for symbol in config.PREDICT_SYMBOLS:
        result = price_predictor.predict_symbol(symbol)
        if not isinstance(result, dict):
            continue  # no trained model yet / not enough recent candles - nothing to track
        logger.log_prediction(symbol, result["prob_up"], result["price"])


def resolve_due_predictions():
    for pred_id, symbol, price_at_prediction, prob_up in logger.get_due_predictions():
        current_price = logger.get_latest_close(symbol)
        if current_price is None:
            continue  # no fresh candle yet - leave unresolved, retried next pass

        predicted_up = prob_up >= 0.5
        actual_up = current_price > price_at_prediction
        correct = predicted_up == actual_up

        move_pct = (current_price - price_at_prediction) / price_at_prediction
        # a "down" call is scored as if it were shorted - direction flips the sign
        raw_pnl = config.TRADE_SIZE_USDT * (move_pct if predicted_up else -move_pct)
        fee_cost = config.TRADE_SIZE_USDT * config.TAKER_FEE * 2  # entry + exit, hypothetical round trip
        pnl_usdt = raw_pnl - fee_cost

        logger.resolve_prediction(pred_id, current_price, correct, pnl_usdt)
