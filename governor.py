"""
Safety governors for live trade execution. executor.py calls check() before
placing a single order of any loop; if it returns not-allowed, the loop is
skipped entirely - no orders touch the exchange.

Five independent checks, any one blocks:
1. Kill switch (governor_state table) - manual on/off via Telegram
   /halt and /resume, and also auto-set by this module (see 2) since a
   cumulative-loss trip or a stuck position needs a human to clear, not a
   timer - it must NOT silently lift on its own.
2. Cumulative loss across all trades ever >= config.MANUAL_REVIEW_LOSS_THRESHOLD_USDT
   - auto-activates the kill switch (persisted, survives restarts) rather
   than just returning not-allowed, so it stays halted until /resume even
   after a redeploy.
3. Today's realized loss >= config.MAX_DAILY_LOSS_USDT - NOT a kill-switch
   trip, just a live query against today's trades, so it naturally lifts at
   UTC midnight without anyone needing to remember to clear it.
4. Trade count today >= config.MAX_TRADES_PER_DAY - same daily auto-reset.
5. Less than config.MIN_SECONDS_BETWEEN_TRADES since the last trade attempt
   (read from Postgres, so this holds across restarts too, not just in-process).

A trade whose profit_usdt couldn't be cleanly computed (executor.py's
ambiguous unwind cases - see its docstring) is exactly as dangerous as a
big loss: the account may be sitting in an untracked position. Any such
trade also auto-activates the kill switch, via record_outcome() below,
regardless of what the known-profit numbers say.
"""

from datetime import datetime, timezone
import requests
import config
import logger


def check() -> tuple:
    """Returns (allowed: bool, reason: str). reason is '' when allowed."""
    state = logger.get_kill_switch()
    if state["killed"]:
        return False, f"Kill switch is ON: {state['reason']}"

    cumulative_loss = -logger.get_cumulative_profit_usdt()
    if cumulative_loss >= config.MANUAL_REVIEW_LOSS_THRESHOLD_USDT:
        reason = (f"Cumulative loss ${cumulative_loss:.2f} >= manual-review threshold "
                  f"${config.MANUAL_REVIEW_LOSS_THRESHOLD_USDT:.2f} - halted, needs a human look.")
        _trip_kill_switch(reason)
        return False, reason

    today = logger.get_todays_trade_stats()
    if today["unknown_profit_count"] > 0:
        reason = (f"{today['unknown_profit_count']} trade(s) today with unknown P&L "
                  f"(ambiguous unwind - account may hold an untracked position) - halted, needs a human look.")
        _trip_kill_switch(reason)
        return False, reason

    today_loss = -today["known_profit_usdt"]
    if today_loss >= config.MAX_DAILY_LOSS_USDT:
        return False, f"Daily loss ${today_loss:.2f} >= limit ${config.MAX_DAILY_LOSS_USDT:.2f} - halted for today."

    if today["count"] >= config.MAX_TRADES_PER_DAY:
        return False, f"Already at {today['count']} trades today (limit {config.MAX_TRADES_PER_DAY})."

    last_trade_at = logger.get_last_trade_time()
    if last_trade_at is not None:
        elapsed = (datetime.now(timezone.utc) - last_trade_at).total_seconds()
        if elapsed < config.MIN_SECONDS_BETWEEN_TRADES:
            return False, f"Only {elapsed:.1f}s since the last trade attempt (min {config.MIN_SECONDS_BETWEEN_TRADES}s)."

    return True, ""


def record_outcome(profit_usdt) -> None:
    """
    Call once per trade attempt, right after executor.py logs it, with
    whatever profit_usdt that trade got logged with (may be None - see
    module docstring point 5 / logger.log_trade's NULL-profit convention).
    """
    if profit_usdt is None:
        _trip_kill_switch(
            "Trade logged with unknown P&L (ambiguous unwind - account may hold an "
            "untracked position) - halted automatically, needs a human look."
        )


def _trip_kill_switch(reason: str):
    state = logger.get_kill_switch()
    if state["killed"]:
        return  # already tripped, don't re-notify on every check() call
    logger.set_kill_switch(True, reason)
    _notify_subscribers(f"\U0001F6D1 Trading halted automatically: {reason}\n\nUse /resume once reviewed.")


def _notify_subscribers(text: str):
    """Direct Telegram send, same pattern as analyze_opportunity_model.py's
    _notify_subscribers - governor.py is called from executor.py's sync
    context, not through TelegramNotifier's async send_alert."""
    if not config.TELEGRAM_API_BOT:
        return
    try:
        subscribers = logger.get_subscribers()
    except Exception as e:
        print(f"[governor] couldn't fetch subscribers to notify: {e}")
        return
    for chat_id in subscribers:
        try:
            requests.post(
                f"https://api.telegram.org/bot{config.TELEGRAM_API_BOT}/sendMessage",
                json={"chat_id": chat_id, "text": text[:4096]},
                timeout=10,
            )
        except Exception as e:
            print(f"[governor] Telegram send failed for {chat_id}: {e}")
