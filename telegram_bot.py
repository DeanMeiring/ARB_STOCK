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
from datetime import datetime, timezone
import requests
import config


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
    lines = [f"Opportunities: {stats['total']} total, {stats['last_24h']} in the last 24h"]

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
                print(f"[telegram] poll error: {e}")
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

        self._send(chat_id, "\n".join(lines))
        self._send(chat_id, "Want a BTC price prediction from the trend model?", reply_markup={
            "inline_keyboard": [[
                {"text": "Yes, predict", "callback_data": "predict_btc"},
                {"text": "No thanks", "callback_data": "predict_no"},
            ]]
        })

    def _handle_predict(self, chat_id):
        import logger

        if chat_id not in logger.get_subscribers():
            self._send(chat_id, "Not logged in - send /login <password> first.")
            return
        import price_predictor  # deferred: pandas/xgboost only load when actually needed
        self._send(chat_id, price_predictor.predict_latest())

    def _handle_callback(self, callback):
        callback_id = callback["id"]
        chat_id = callback["message"]["chat"]["id"]
        data = callback.get("data", "")

        self._answer_callback(callback_id)  # stops the button's loading spinner

        if data == "predict_btc":
            self._handle_predict(chat_id)
        # "predict_no" needs no further action

    def _answer_callback(self, callback_id, text=None):
        try:
            payload = {"callback_query_id": callback_id}
            if text:
                payload["text"] = text
            requests.post(f"{self._api_base}/answerCallbackQuery", json=payload, timeout=10)
        except Exception as e:
            print(f"[telegram] answerCallbackQuery error: {e}")

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
            print(f"[telegram] send error: {e}")

    async def notify_opportunity(self, result):
        text = (
            f"[OPPORTUNITY] {result.direction}\n"
            f"profit: {result.profit_pct * 100:.4f}% "
            f"(${result.profit_usdt:.2f} on ${result.start_usdt:.0f})"
        )
        await self.send_alert(text)

    async def send_alert(self, text: str):
        """Send text to every logged-in chat - used for opportunities, the
        stale-connection watchdog, and the daily heartbeat alike."""
        if not config.TELEGRAM_API_BOT:
            return
        import logger

        subscribers = await asyncio.to_thread(logger.get_subscribers)
        for chat_id in subscribers:
            await asyncio.to_thread(self._send, chat_id, text)
