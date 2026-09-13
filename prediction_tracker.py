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
- If there IS an open position: two circuit breakers are checked FIRST,
  regardless of what prob_up currently says - config.STOP_LOSS_PCT (force
  sell if price has dropped that fraction or more from entry) and
  config.TAKE_PROFIT_NET_PCT (force sell once the fee-adjusted net gain
  reaches that fraction of TRADE_SIZE_USDT - "100% including fees", not
  just a 100% raw price move). Only if neither fires does the normal signal
  logic apply: sells the moment prob_up drops back below
  PREDICT_UP_THRESHOLD, scoring the hypothetical P&L of having held
  config.TRADE_SIZE_USDT of it for however long the model stayed confident,
  fees included both ways (config.TAKER_FEE, charged on entry and exit).
  Every close records WHY in paper_trades.close_reason ('signal',
  'stop_loss', or 'take_profit').
- Otherwise (still above threshold and already holding, or still below and
  not holding, with neither circuit breaker tripped): does nothing this
  tick - the position's hold time is whatever the model's own confidence
  dictates, not a fixed window.

Note both circuit breakers only run once per PREDICTION_CHECK_INTERVAL_MINUTES
(same hourly cadence as everything else here) - this caps the damage/locks
in gains as of each hourly check, not continuously. A true sub-hour
"never drops more than 50% within the hour" guarantee would need faster
polling than this architecture provides today.

logger.get_todays_paper_trade_stats() is what /predict and the dashboard
read to show today's win rate and hypothetical P&L.

Every symbol also gets a row in logger.prediction_snapshots on every check,
whether or not it crossed the buy threshold - see
logger.get_prediction_vs_actual for how the dashboard turns that into a
predicted-vs-actual-price-an-hour-later comparison.
"""

from datetime import datetime, timezone
import config
import logger
import web


def check_signals(strategy: str = "absolute"):
    """strategy: 'absolute' (default - is-price-going-up, what /predict
    shows) or 'relative' (cross-sectional - does-this-coin-beat-the-basket,
    see analyze_price_trend_model.build_features_relative). Runs the exact
    same paper-trading mechanics either way - only which model gets asked
    for prob_up and which strategy-tagged rows get written/read changes;
    see logger.init_prediction_tracking's docstring on how the two stay
    apart within the same tables. Deferred import below - same reasoning as
    telegram_bot.py's /predict handler: pandas/xgboost only load when a
    prediction is actually needed, never in the always-on detector's own
    import path."""
    import price_predictor
    predict_fn = price_predictor.predict_symbol if strategy == "absolute" else price_predictor.predict_symbol_relative
    snapshot_dict = web.last_check_snapshot if strategy == "absolute" else web.last_check_snapshot_relative

    print(f"[prediction-tracker:{strategy}] checking {len(config.PREDICT_SYMBOLS)} symbols "
          f"(threshold {config.PREDICT_UP_THRESHOLD*100:.0f}%)...")

    snapshot = []
    checked_at = datetime.now(timezone.utc)

    for symbol in config.PREDICT_SYMBOLS:
        result = predict_fn(symbol)
        if not isinstance(result, dict):
            print(f"[prediction-tracker:{strategy}] {symbol}: skipped - {result}")
            continue  # no trained model yet / not enough recent candles - nothing to act on

        prob_up, price = result["prob_up"], result["price"]
        snapshot.append({"symbol": symbol, "prob_up": prob_up})
        open_trade = logger.get_open_paper_trade(symbol, strategy)
        signal_up = prob_up >= config.PREDICT_UP_THRESHOLD
        logger.log_prediction_snapshot(symbol, checked_at, price, prob_up, signal_up, strategy)

        if open_trade is None:
            if signal_up:
                logger.open_paper_trade(symbol, price, prob_up, strategy)
                print(f"[prediction-tracker:{strategy}] BUY {symbol} @ {price} (prob_up {prob_up*100:.1f}%)")
            else:
                print(f"[prediction-tracker:{strategy}] {symbol}: prob_up {prob_up*100:.1f}%, "
                      f"no position, below threshold")
            continue

        # open_trade exists past this point - same fee-adjusted P&L feeds
        # all three possible exits below (stop-loss/take-profit/signal),
        # checked in that priority order regardless of what prob_up says -
        # see config.STOP_LOSS_PCT / TAKE_PROFIT_NET_PCT.
        move_pct = (price - open_trade["entry_price"]) / open_trade["entry_price"]
        fee_cost = config.TRADE_SIZE_USDT * config.TAKER_FEE * 2  # entry + exit, hypothetical round trip
        pnl_usdt = config.TRADE_SIZE_USDT * move_pct - fee_cost
        net_pct = pnl_usdt / config.TRADE_SIZE_USDT  # move_pct minus the round-trip fee, as a fraction

        if move_pct <= -config.STOP_LOSS_PCT:
            logger.close_paper_trade(open_trade["id"], price, prob_up, pnl_usdt, reason="stop_loss")
            print(f"[prediction-tracker:{strategy}] STOP-LOSS SELL {symbol} @ {price} (prob_up {prob_up*100:.1f}%) "
                  f"- entry {open_trade['entry_price']}, dropped {move_pct*100:.1f}%, pnl ${pnl_usdt:.2f}")
        elif net_pct >= config.TAKE_PROFIT_NET_PCT:
            logger.close_paper_trade(open_trade["id"], price, prob_up, pnl_usdt, reason="take_profit")
            print(f"[prediction-tracker:{strategy}] TAKE-PROFIT SELL {symbol} @ {price} (prob_up {prob_up*100:.1f}%) "
                  f"- entry {open_trade['entry_price']}, net gain {net_pct*100:.1f}% (incl. fees), pnl ${pnl_usdt:.2f}")
        elif not signal_up:
            logger.close_paper_trade(open_trade["id"], price, prob_up, pnl_usdt, reason="signal")
            print(f"[prediction-tracker:{strategy}] SELL {symbol} @ {price} (prob_up {prob_up*100:.1f}%) "
                  f"- entry {open_trade['entry_price']}, pnl ${pnl_usdt:.2f}")
        else:
            print(f"[prediction-tracker:{strategy}] {symbol}: prob_up {prob_up*100:.1f}%, still holding "
                  f"(entry {open_trade['entry_price']})")

    snapshot_dict["checked_at"] = checked_at
    snapshot_dict["predictions"] = sorted(snapshot, key=lambda r: r["prob_up"], reverse=True)
