#!/usr/bin/env python3
"""Read-only local-network dashboard for garmin-ai-coach: shows the latest
advice, advice history, core metric charts, recent activities, and an
adherence timeline (advice vs. what was actually done). Reuses coach.py's
existing InfluxDB query functions and coach_log — no separate query layer."""

import json
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import coach

DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8420"))
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


def build_payload() -> dict:
    history = coach.read_coach_log()
    metrics = coach.build_metrics()
    metrics["vo2max_series"] = coach.vo2max_series(28)
    return {
        "latest": history[-1] if history else None,
        "history": history,
        "metrics": metrics,
        "generated_at": datetime.now(coach.LOCAL_TZ).isoformat(),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep container logs quiet — this is a low-traffic local page

    def do_GET(self):
        if self.path == "/":
            self._serve_html()
        elif self.path == "/api/data":
            self._serve_json()
        else:
            self.send_error(404)

    def _serve_html(self):
        with open(HTML_FILE, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self):
        try:
            payload = build_payload()
            status = 200
        except Exception as e:
            payload = {"error": str(e)}
            status = 502
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", DASHBOARD_PORT), Handler)
    print(f"Dashboard listening on :{DASHBOARD_PORT}")
    server.serve_forever()
