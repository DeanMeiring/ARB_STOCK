"""
Minimal Telegram notifier - no execution, no external bot framework.

Polls Telegram's getUpdates for incoming messages. Anyone who sends
/login <TELEGRAM_LOGIN_PASSWORD> gets their chat added to logger's
telegram_subscribers table and starts receiving opportunity alerts.
/report (logged-in only) replies with summary stats from Postgres, then
offers a BTC price prediction via an inline "Yes/No" button - /predict
does the same thing directly, without needing to go through /report first.
There is no way to remove a subscriber yet - if that's ever needed,
delete the row directly in Postgres.

Runs alongside the price stream as a separate asyncio task; blocking
HTTP calls are pushed to a thread so they never stall the WS loop.
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
import requests
import config

_DURATION_RE = re.compile(r"^(\d+)\s*(m|min|mins|h|hr|hrs|hour|hours|d|day|days)?$", re.IGNORECASE)
_DURATION_UNITS = {
    None: "minutes", "m": "minutes", "min": "minutes", "mins": "minutes",
    "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
    "d": "days", "day": "days", "days": "days",
}


def _parse_duration(text: str):
    """'90' -> 90 minutes, '2h' -> 2 hours, '1d' -> 1 day, etc. - used by
    /pause <duration>. Returns a timedelta, or None if unparseable."""
    match = _DURATION_RE.match(text.strip())
    if not match:
        return None
    amount, unit = match.groups()
    return timedelta(**{_DURATION_UNITS[unit.lower() if unit else None]: int(amount)})


def _format_ago(ts):
    seconds = (datetime.now(timezone.utc) - ts).total_seconds()
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _format_report_section(logger_module, source: str) -> list:
    """Build the lines for one detector's slice of /report - shared between
    triangular and cross_exchange so the format can't drift between them."""
    stats = logger_module.get_report_stats(source)
    pnl = logger_module.get_arb_pnl_stats(source)
    lines = [f"Opportunities: {stats['total']} total, {stats['last_24h']} in the last 24h"]
    lines.append(
        f"Hypothetical P&L: +${pnl['total_pnl_usdt']:.2f} lifetime, "
        f"+${pnl['last_24h_pnl_usdt']:.2f} last 24h (idealized - see dashboard for caveats)"
    )

    if stats["latest"]:
        direction, profit_pct, profit_usdt, ts = stats["latest"]
        lines.append(
            f"Most recent: {direction}\n"
            f"  {profit_pct * 100:.4f}% (${profit_usdt:.2f}) — {_format_ago(ts)}"
        )
    else:
        lines.append("Most recent: none logged yet")

    if stats["best"]:
        direction, profit_pct, profit_usdt, ts = stats["best"]
        lines.append(
            f"Best ever: {direction}\n"
            f"  {profit_pct * 100:.4f}% (${profit_usdt:.2f}) — {ts:%b %-d, %H:%M} UTC"
        )
    else:
        lines.append("Best ever: none logged yet")

    closest = logger_module.get_closest_miss_24h(source)
    if closest:
        direction, profit_pct, profit_usdt, ts = closest
        shortfall = config.MIN_PROFIT_THRESHOLD - profit_pct
        lines.append(
            f"Closest miss (last 24h): {direction}\n"
            f"  {profit_pct * 100:.4f}% — {shortfall * 100:.4f}% short of threshold\n"
            f"  {_format_ago(ts)} ({ts:%b %-d, %H:%M} UTC)"
        )
    else:
        lines.append("Closest miss (last 24h): no data yet")

    return lines


class TelegramNotifier:
    def __init__(self):
        self._api_base = f"https://api.telegram.org/bot{config.TELEGRAM_API_BOT}"
        self._offset = 0

    def _redact(self, message: str) -> str:
        """requests exceptions stringify the full request URL, which embeds
        our bot token via self._api_base - strip it before ever printing an
        exception, so it doesn't end up sitting in Railway's logs (which it
        did, repeatedly, before this)."""
        return message.replace(config.TELEGRAM_API_BOT, "***") if config.TELEGRAM_API_BOT else message

    async def poll_forever(self):
        if not config.TELEGRAM_API_BOT:
            print("[telegram] TELEGRAM_API_BOT not set - notifications disabled")
            return
        while True:
            try:
                updates = await asyncio.to_thread(self._get_updates)
                for update in updates:
                    self._handle_update(update)
            except Exception as e:
                print(f"[telegram] poll error: {self._redact(str(e))}")
                await asyncio.sleep(5)

    def _get_updates(self):
        resp = requests.get(
            f"{self._api_base}/getUpdates",
            params={"offset": self._offset, "timeout": 25},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])

    def _handle_update(self, update):
        self._offset = update["update_id"] + 1

        callback = update.get("callback_query")
        if callback:
            self._handle_callback(callback)
            return

        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat_id = message.get("chat", {}).get("id")
        if not chat_id or not text:
            return

        if text.startswith("/login"):
            self._handle_login(chat_id, text)
        elif text.startswith("/report"):
            self._handle_report(chat_id)
        elif text.startswith("/predict"):
            self._handle_predict(chat_id)
        elif text.startswith("/trainstatus"):
            self._handle_trainstatus(chat_id)
        elif text.startswith("/tradestatus"):
            self._handle_tradestatus(chat_id)
        elif text.startswith("/halt"):
            self._handle_halt(chat_id, text)
        elif text.startswith("/resume"):
            self._handle_resume(chat_id)
        elif text.startswith("/unpause"):
            self._handle_unpause(chat_id)
        elif text.startswith("/pause"):
            self._handle_pause(chat_id, text)

    def _handle_login(self, chat_id, text):
        import logger  # deferred to avoid a hard import-time DB dependency

        parts = text.split(maxsplit=1)
        password = parts[1].strip() if len(parts) > 1 else ""

        if not config.TELEGRAM_LOGIN_PASSWORD:
            self._send(chat_id, "Login is not configured yet.")
        elif password == config.TELEGRAM_LOGIN_PASSWORD:
            logger.add_subscriber(chat_id)
            self._send(chat_id, "Logged in. You'll get an alert here when a real arbitrage opportunity clears threshold.")
        else:
            self._send(chat_id, "Wrong password.")

    def _handle_report(self, chat_id):
        import logger

        if chat_id not in logger.get_subscribers():
            self._send(chat_id, "Not logged in - send /login <password> first.")
            return

        lines = [
            "📊 ARB_STOCK Report",
            f"Threshold: {config.MIN_PROFIT_THRESHOLD * 100:.4f}% (net of fees, both detectors)",
            "",
            "── Triangular (Binance) ──",
            *_format_report_section(logger, "triangular"),
            "",
            "── Cross-Exchange (Binance vs Crypto.com) ──",
            *_format_report_section(logger, "cross_exchange"),
            "",
            f"Subscribers: {len(logger.get_subscribers())}",
        ]
        if config.DASHBOARD_URL:
            lines += ["", f"📈 Live dashboard: {config.DASHBOARD_URL}"]

        self._send(chat_id, "\n".join(lines))
        self._send(chat_id, "Want price-trend predictions across all tracked coins?", reply_markup={
            "inline_keyboard": [[
                {"text": "Yes, predict", "callback_data": "predict_all"},
                {"text": "No thanks", "callback_data": "predict_no"},
            ]]
        })

    def _handle_predict(self, chat_id):
        import logger

        if chat_id not in logger.get_subscribers():
            self._send(chat_id, "Not logged in - send /login <password> first.")
            return
        import price_predictor  # deferred: pandas/xgboost only load when actually needed
        text = price_predictor.predict_all_text()

        stats = logger.get_todays_paper_trade_stats()
        if stats["total"]:
            text += (
                f"\n\n📈 Paper trades closed today: {stats['correct']}/{stats['total']} profitable "
                f"({stats['accuracy_pct']:.0f}%), hypothetical P&L (${config.TRADE_SIZE_USDT:.0f}/trade): "
                f"${stats['pnl_usdt']:.2f}"
            )
        else:
            text += "\n\n📈 No paper trades closed yet today - one opens automatically once a coin crosses the threshold."
        if stats["open_count"]:
            text += f"\n{stats['open_count']} coin(s) currently in an open paper position."

        if config.DASHBOARD_URL:
            text += f"\n\n📈 Live dashboard: {config.DASHBOARD_URL}"
        self._send(chat_id, text)

    def _handle_trainstatus(self, chat_id):
        """Reports the last recorded outcome of the training cron job -
        read from Postgres (logger.get_latest_training_run), not from the
        cron service's own Telegram send, which can silently no-op if its
        TELEGRAM_API_BOT var isn't set up right. Works regardless of that."""
        import logger

        if chat_id not in logger.get_subscribers():
            self._send(chat_id, "Not logged in - send /login <password> first.")
            return

        row = logger.get_latest_training_run()
        if not row:
            self._send(chat_id, "No training run recorded yet - the daily cron hasn't run (or reached the point of recording its outcome) since this tracking was added.")
            return

        ran_at, status, detail = row
        header = f"Last training run: {status.upper()} — {_format_ago(ran_at)} ({ran_at:%b %-d, %H:%M} UTC)"
        body = f"\n\n{detail}" if detail else ""
        self._send(chat_id, (header + body)[:4096])

    def _handle_tradestatus(self, chat_id):
        """Live-trading governor state - kill switch, today's/cumulative P&L
        and trade count, against the config.py limits. Reads straight from
        Postgres (governor.py's own state), so this is accurate even if
        EXECUTE_TRADES is off (shows what WOULD gate trades if it were on)."""
        import logger

        if chat_id not in logger.get_subscribers():
            self._send(chat_id, "Not logged in - send /login <password> first.")
            return

        state = logger.get_kill_switch()
        today = logger.get_todays_trade_stats()
        cumulative = logger.get_cumulative_profit_usdt()

        lines = [
            "\U0001F6E1 Trade Governor Status",
            f"Live execution: {'ON' if config.EXECUTE_TRADES else 'OFF'} ({config.BINANCE_BASE_URL})",
            f"Kill switch: {'ON — ' + state['reason'] if state['killed'] else 'off'}",
            "",
            f"Today: {today['count']} trade(s), known P&L ${today['known_profit_usdt']:.2f} "
            f"(limit -${config.MAX_DAILY_LOSS_USDT:.2f}, {config.MAX_TRADES_PER_DAY} trades/day)",
        ]
        if today["unknown_profit_count"]:
            lines.append(f"  ⚠ {today['unknown_profit_count']} trade(s) today with UNKNOWN P&L")
        lines.append(
            f"Cumulative: ${cumulative:.2f} (manual-review halt at -${config.MANUAL_REVIEW_LOSS_THRESHOLD_USDT:.2f})"
        )
        lines.append("\nCommands: /halt <reason>, /resume")
        self._send(chat_id, "\n".join(lines))

    def _handle_halt(self, chat_id, text):
        import logger

        if chat_id not in logger.get_subscribers():
            self._send(chat_id, "Not logged in - send /login <password> first.")
            return

        parts = text.split(maxsplit=1)
        reason = parts[1].strip() if len(parts) > 1 else "manually halted via /halt"
        logger.set_kill_switch(True, reason)
        self._send(chat_id, f"\U0001F6D1 Kill switch ON: {reason}\n\nNo further trades will execute until /resume.")

    def _handle_resume(self, chat_id):
        import logger

        if chat_id not in logger.get_subscribers():
            self._send(chat_id, "Not logged in - send /login <password> first.")
            return

        logger.set_kill_switch(False, None)
        self._send(chat_id, "✅ Kill switch OFF. Trading governor checks (daily loss limit, rate limiter, etc.) still apply.")

    def _handle_pause(self, chat_id, text):
        """/pause (no args) - pause paper trading indefinitely (the "off
        switch"). /pause <duration> (e.g. 90, 90m, 2h, 1d - bare number is
        minutes) - pause for that long, auto-resuming on its own (see
        logger.get_pause_state). Freezes prediction_tracker.check_signals()
        entirely for BOTH strategies - no buys, sells, stop-loss/take-profit,
        or prediction logging - not just new entries."""
        import logger

        if chat_id not in logger.get_subscribers():
            self._send(chat_id, "Not logged in - send /login <password> first.")
            return

        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not arg:
            logger.set_pause(True, None, "manually paused via /pause")
            self._send(chat_id, "⏸ Paper trading PAUSED indefinitely (both strategies) - no buys, sells, "
                                 "or circuit breakers until /unpause. Everything else (detection, dashboard) "
                                 "keeps running.")
            return

        duration = _parse_duration(arg)
        if duration is None:
            self._send(chat_id, f"Couldn't parse '{arg}' as a duration - try a plain number of minutes (90), "
                                 "or with a unit (90m, 2h, 1d).")
            return

        paused_until = datetime.now(timezone.utc) + duration
        logger.set_pause(True, paused_until, f"paused via /pause {arg}")
        self._send(chat_id, f"⏸ Paper trading PAUSED until {paused_until.strftime('%H:%M UTC')} "
                             f"(~{int(duration.total_seconds() // 60)} min) - auto-resumes then, or send /unpause sooner.")

    def _handle_unpause(self, chat_id):
        import logger

        if chat_id not in logger.get_subscribers():
            self._send(chat_id, "Not logged in - send /login <password> first.")
            return

        logger.set_pause(False, None, None)
        self._send(chat_id, "▶ Paper trading RESUMED - normal buy/sell/circuit-breaker logic applies again.")

    def _handle_callback(self, callback):
        callback_id = callback["id"]
        chat_id = callback["message"]["chat"]["id"]
        data = callback.get("data", "")

        self._answer_callback(callback_id)  # stops the button's loading spinner

        if data == "predict_all":
            self._handle_predict(chat_id)
        # "predict_no" needs no further action

    def _answer_callback(self, callback_id, text=None):
        try:
            payload = {"callback_query_id": callback_id}
            if text:
                payload["text"] = text
            requests.post(f"{self._api_base}/answerCallbackQuery", json=payload, timeout=10)
        except Exception as e:
            print(f"[telegram] answerCallbackQuery error: {self._redact(str(e))}")

    def _send(self, chat_id, text, reply_markup=None):
        try:
            payload = {"chat_id": chat_id, "text": text}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            requests.post(
                f"{self._api_base}/sendMessage",
                json=payload,
                timeout=10,
            )
        except Exception as e:
            print(f"[telegram] send error: {self._redact(str(e))}")

    async def send_alert(self, text: str):
        """Send text to every logged-in chat - used for the stale-connection
        watchdog and the daily heartbeat. Arbitrage opportunities used to
        push an alert here too (notify_opportunity, removed) - a real
        crossing can re-trigger on every tick while conditions hold, which
        turned into a Telegram spam burst rather than a useful alert. They're
        still logged/counted (main.py's log_opportunity calls); check /report
        or the dashboard's Arbitrage P&L card for cumulative $ instead."""
        if not config.TELEGRAM_API_BOT:
            return
        import logger

        subscribers = await asyncio.to_thread(logger.get_subscribers)
        for chat_id in subscribers:
            await asyncio.to_thread(self._send, chat_id, text)
