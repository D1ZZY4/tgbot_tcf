# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Vercel Cron maintenance endpoint (route: /api/cron, see vercel.json)."""

from __future__ import annotations

import hmac
import logging
from http.server import BaseHTTPRequestHandler

from tcbot import cfg
from tcbot.serverless import run, run_warn_expiry

log = logging.getLogger(__name__)


class handler(BaseHTTPRequestHandler):
    """Run scheduled maintenance (warn expiry); replaces APScheduler on Vercel."""

    def _reply(self, status: int, body: str) -> None:
        """Send a plain-text response."""
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        """Verify the cron secret, then prune expired warns when enabled."""
        # * Vercel Cron sends `Authorization: Bearer <CRON_SECRET>` when the
        # * project has CRON_SECRET configured.  Enforcement is fail-open only
        # * when the operator left CRON_SECRET empty; production must set it.
        secret = cfg.cron_secret
        if secret:
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth, f"Bearer {secret}"):
                log.warning("cron: rejected request with invalid bearer token.")
                self._reply(401, "Unauthorized")
                return
        else:
            log.warning("cron: CRON_SECRET is not configured; endpoint is open.")

        try:
            status, body = run(run_warn_expiry())
        except Exception:
            log.exception("cron: instance loop failed to run warn expiry.")
            self._reply(500, "Internal error")
            return
        self._reply(status, body)
