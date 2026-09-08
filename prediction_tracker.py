"""
Paper trading on the model's own signal - simulates one long position per
coin, entered and exited purely by the model's own confidence crossing
config.PREDICT_UP_THRESHOLD, not a fixed clock. No order is ever placed
here; this is entirely independent of executor.py/governor.py.

Every config.PREDICTION_CHECK_INTERVAL_MINUTES, main.py's
prediction_tracking_loop() calls check_signals(), which for each
config.PREDICT_SYMBOLS coin:

- If there's no open paper position and the current prob_up >=
  PREDICT_UP_THRESHOLD: "buys" - opens one (logger.open_paper_trade) at the
  current price.
- If there IS an open position and prob_up has dropped back below
  PREDICT_UP_THRESHOLD: "sells" - closes it (logger.close_paper_trade),
  scoring the hypothetical P&L of having held config.TRADE_SIZE_USDT of it
  for however long the model stayed confident, fees included both ways
  (config.TAKER_FEE, charged on entry and exit).
- Otherwise (still above threshold and already holding, or still below and
  not holding): does nothing this tick - the position's hold time is
  whatever the model's own confidence dictates, not a fixed window.

logger.get_todays_paper_trade_stats() is what /predict and the dashboard
read to show today's win rate and hypothetical P&L.
"""

import config
import logger


def check_signals():
    """Deferred import - same reasoning as telegram_bot.py's /predict
    handler: pandas/xgboost only load when a prediction is actually needed,
    never in the always-on detector's own import path."""
    import price_predictor

    print(f"[prediction-tracker] checking {len(config.PREDICT_SYMBOLS)} symbols "
          f"(threshold {config.PREDICT_UP_THRESHOLD*100:.0f}%)...")

    for symbol in config.PREDICT_SYMBOLS:
        result = price_predictor.predict_symbol(symbol)
        if not isinstance(result, dict):
            print(f"[prediction-tracker] {symbol}: skipped - {result}")
            continue  # no trained model yet / not enough recent candles - nothing to act on

        prob_up, price = result["prob_up"], result["price"]
        open_trade = logger.get_open_paper_trade(symbol)
        signal_up = prob_up >= config.PREDICT_UP_THRESHOLD

        if open_trade is None:
            if signal_up:
                logger.open_paper_trade(symbol, price, prob_up)
                print(f"[prediction-tracker] BUY {symbol} @ {price} (prob_up {prob_up*100:.1f}%)")
            else:
                print(f"[prediction-tracker] {symbol}: prob_up {prob_up*100:.1f}%, no position, below threshold")
        elif not signal_up:
            move_pct = (price - open_trade["entry_price"]) / open_trade["entry_price"]
            raw_pnl = config.TRADE_SIZE_USDT * move_pct
            fee_cost = config.TRADE_SIZE_USDT * config.TAKER_FEE * 2  # entry + exit, hypothetical round trip
            pnl_usdt = raw_pnl - fee_cost
            logger.close_paper_trade(open_trade["id"], price, prob_up, pnl_usdt)
            print(f"[prediction-tracker] SELL {symbol} @ {price} (prob_up {prob_up*100:.1f}%) "
                  f"- entry {open_trade['entry_price']}, pnl ${pnl_usdt:.2f}")
        else:
            print(f"[prediction-tracker] {symbol}: prob_up {prob_up*100:.1f}%, still holding "
                  f"(entry {open_trade['entry_price']})")
