"""
TEMPORARY diagnostic-only HTTP server - not part of the app. Exists solely
to answer one question directly (via curl, self-serve, no Telegram/logs
needed): from THIS service, does DATABASE_URL resolve/connect, what's
actually in training_runs and trained_models, and does a Telegram send
actually succeed. Long-running on purpose, unlike the training cron
script, so it sidesteps the empty-deploy-logs mystery entirely.

Delete this file and revert opportunity-model's startCommand/cronSchedule
once the real issue is found - this is not meant to stay deployed.
"""

import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import config


def _gather_status(send_telegram: bool) -> dict:
    out = {}
    out["database_url_present"] = bool(config.DATABASE_URL)
    out["database_url_len"] = len(config.DATABASE_URL) if config.DATABASE_URL else 0
    out["database_url_prefix"] = config.DATABASE_URL[:20] if config.DATABASE_URL else None
    out["telegram_bot_present"] = bool(config.TELEGRAM_API_BOT)
    out["telegram_bot_len"] = len(config.TELEGRAM_API_BOT) if config.TELEGRAM_API_BOT else 0

    try:
        import psycopg2
        conn = psycopg2.connect(config.DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT current_database(), inet_server_addr()::text, inet_server_port()")
        out["pg_connect_ok"] = True
        out["pg_identity"] = cur.fetchone()

        cur.execute("SELECT to_regclass('public.training_runs')")
        out["training_runs_table_exists"] = cur.fetchone()[0] is not None

        cur.execute("SELECT to_regclass('public.trained_models')")
        out["trained_models_table_exists"] = cur.fetchone()[0] is not None

        if out["training_runs_table_exists"]:
            cur.execute("SELECT COUNT(*) FROM training_runs")
            out["training_runs_count"] = cur.fetchone()[0]
            cur.execute("SELECT ran_at, status, LEFT(COALESCE(detail,''), 300) FROM training_runs ORDER BY ran_at DESC LIMIT 5")
            out["training_runs_latest"] = [[str(r[0]), r[1], r[2]] for r in cur.fetchall()]

        if out["trained_models_table_exists"]:
            cur.execute("SELECT model_name, trained_at FROM trained_models ORDER BY trained_at DESC")
            out["trained_models"] = [[r[0], str(r[1])] for r in cur.fetchall()]

        cur.execute("SELECT COUNT(*) FROM market_candles WHERE symbol = 'BTCUSDT'")
        out["btcusdt_candle_count"] = cur.fetchone()[0]

        # Write-permission probe - separate from whether reads work.
        cur.execute("""
            INSERT INTO training_runs (ran_at, status, detail)
            VALUES (NOW(), 'diagnostic_probe', 'written by diagnostic_server.py')
        """)
        conn.commit()
        out["write_probe_ok"] = True

        cur.close()
        conn.close()
    except Exception:
        out["pg_error"] = traceback.format_exc()

    if send_telegram and config.TELEGRAM_API_BOT:
        try:
            import requests
            import logger
            subs = logger.get_subscribers()
            out["telegram_subscribers"] = subs
            results = []
            for chat_id in subs:
                resp = requests.post(
                    f"https://api.telegram.org/bot{config.TELEGRAM_API_BOT}/sendMessage",
                    json={"chat_id": chat_id, "text": "diagnostic_server.py test ping"},
                    timeout=10,
                )
                results.append({"chat_id": chat_id, "status_code": resp.status_code, "body": resp.text[:500]})
            out["telegram_send_results"] = results
        except Exception:
            out["telegram_error"] = traceback.format_exc()

    return out


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/status":
            self.send_response(404)
            self.end_headers()
            return
        qs = parse_qs(parsed.query)
        send_telegram = qs.get("telegram", ["0"])[0] == "1"
        body = json.dumps(_gather_status(send_telegram), indent=2, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # quiet - we don't care about server logs here, only the response body


if __name__ == "__main__":
    # Deploy logs are only ever captured for genuinely long-running
    # processes on this service - confirmed by "Starting Container" /
    # "listening" actually showing up here, unlike every quick-exit run of
    # the training script. So run the check at startup and print it, where
    # it will actually be visible via get-logs, instead of only on-demand
    # via the HTTP handler (which still needs a request that reaches it).
    print("=== STARTUP DIAGNOSTIC ===")
    print(json.dumps(_gather_status(send_telegram=True), indent=2, default=str))
    print("=== END STARTUP DIAGNOSTIC ===")

    port = int(os.environ.get("PORT", "8080"))
    print(f"Diagnostic server listening on 0.0.0.0:{port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
