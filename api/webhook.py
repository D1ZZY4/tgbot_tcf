# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Vercel serverless Telegram webhook receiver (route: /api/webhook)."""

from __future__ import annotations

import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler

from tcbot import cfg
from tcbot.serverless import handle_telegram_payload, run

log = logging.getLogger(__name__)

# * Telegram secret header; same contract as the Flask receiver in alive.py.
_SECRET_HEADER: str = "X-Telegram-Bot-Api-Secret-Token"


class handler(BaseHTTPRequestHandler):
    """Receive one Telegram update per invocation and process it synchronously."""

    def _reply(self, status: int, body: str) -> None:
        """Send a plain-text response."""
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        """Liveness probe so operators (and Vercel checks) can verify the route."""
        self._reply(200, "OK")

    def do_POST(self) -> None:
        """Validate the secret, decode the update, and process it via PTB."""
        # ! CRITICAL: WEBHOOK_SECRET must be explicitly configured on Vercel.
        # ! cfg.webhook_secret auto-generates a random token when unset, which
        # ! changes on every cold start and can never match Telegram's copy,
        # ! so the explicit value (cfg.webhook_secret_explicit) is checked here
        # ! and the endpoint fails closed when it is missing.
        expected = cfg.webhook_secret_explicit
        if not expected:
            log.error("webhook: WEBHOOK_SECRET is not configured; refusing update.")
            self._reply(503, "webhook secret not configured")
            return
        token = self.headers.get(_SECRET_HEADER, "")
        if not hmac.compare_digest(token, expected):
            log.warning("webhook: rejected request with invalid secret token.")
            self._reply(403, "Forbidden")
            return

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("webhook: received request with non-JSON body.")
            self._reply(400, "Bad request")
            return
        except UnicodeDecodeError:
            log.warning("webhook: received request with non-JSON body.")
            self._reply(400, "Bad request")
            return
        if not isinstance(data, dict):
            log.warning("webhook: received JSON body that is not an object.")
            self._reply(400, "Bad request")
            return

        try:
            status, body = run(handle_telegram_payload(data))
        except Exception:
            log.exception("webhook: instance loop failed to run update handler.")
            self._reply(500, "Internal error")
            return
        self._reply(status, body)
