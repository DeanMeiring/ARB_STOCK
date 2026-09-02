"""
Minimal Telegram notifier - no execution, no external bot framework.

Polls Telegram's getUpdates for incoming messages. Anyone who sends
/login <TELEGRAM_LOGIN_PASSWORD> gets their chat added to logger's
telegram_subscribers table and starts receiving opportunity alerts.
/report (logged-in only) replies with summary stats from Postgres.
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
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat_id = message.get("chat", {}).get("id")
        if not chat_id or not text:
            return

        if text.startswith("/login"):
            self._handle_login(chat_id, text)
        elif text.startswith("/report"):
            self._handle_report(chat_id)

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

        stats = logger.get_report_stats()
        lines = [
            "📊 ARB_STOCK Report",
            "",
            f"Opportunities: {stats['total']} total, {stats['last_24h']} in the last 24h",
            f"Threshold: {config.MIN_PROFIT_THRESHOLD * 100:.4f}%",
            "",
        ]

        if stats["latest"]:
            direction, profit_pct, profit_usdt, ts = stats["latest"]
            lines.append(
                f"Most recent: {direction}\n"
                f"  {profit_pct * 100:.4f}% (${profit_usdt:.2f}) — {_format_ago(ts)}"
            )
        else:
            lines.append("Most recent: none logged yet")
        lines.append("")

        if stats["best"]:
            direction, profit_pct, profit_usdt, ts = stats["best"]
            lines.append(
                f"Best ever: {direction}\n"
                f"  {profit_pct * 100:.4f}% (${profit_usdt:.2f}) — {ts:%b %-d, %H:%M} UTC"
            )
        else:
            lines.append("Best ever: none logged yet")
        lines.append("")

        closest = logger.get_closest_miss_24h()
        if closest:
            direction, profit_pct, profit_usdt, ts = closest
            shortfall = config.MIN_PROFIT_THRESHOLD - profit_pct
            lines.append(
                f"Closest miss (last 24h): {direction}\n"
                f"  {profit_pct * 100:.4f}% — {shortfall * 100:.4f}% short of threshold, "
                f"{_format_ago(ts)}"
            )
        else:
            lines.append("Closest miss (last 24h): no data yet")
        lines.append("")

        lines.append(f"Subscribers: {stats['subscribers']}")

        self._send(chat_id, "\n".join(lines))

    def _send(self, chat_id, text):
        try:
            requests.post(
                f"{self._api_base}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
        except Exception as e:
            print(f"[telegram] send error: {e}")

    async def notify_opportunity(self, result):
        if not config.TELEGRAM_API_BOT:
            return
        import logger

        subscribers = await asyncio.to_thread(logger.get_subscribers)
        if not subscribers:
            return
        text = (
            f"[OPPORTUNITY] {result.direction}\n"
            f"profit: {result.profit_pct * 100:.4f}% "
            f"(${result.profit_usdt:.2f} on ${result.start_usdt:.0f})"
        )
        for chat_id in subscribers:
            await asyncio.to_thread(self._send, chat_id, text)
