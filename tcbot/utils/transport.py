# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Shared Telegram transport tuning for every runtime entry point.

Single owner for the outbound HTTP timeouts and connection pool sizes
used by the PTB ``ApplicationBuilder``. ``tcbot/__main__.py`` (webhook and
polling) and ``tcbot/serverless.py`` (Vercel) must behave identically here;
a second copy drifts silently, so both import these instead of redefining
them.
"""

from __future__ import annotations

# * HTTP timeout values for the PTB ApplicationBuilder (seconds).
# * Raised for Replit: first getMe() response can take >15s on this network.
HTTP_READ_TIMEOUT: int = 60
HTTP_WRITE_TIMEOUT: int = 30
HTTP_CONNECT_TIMEOUT: int = 30
HTTP_POOL_TIMEOUT: int = 15

# * Connection pool size for the underlying httpx client (API calls).
# * Not used for update fetching in webhook mode; still needed for send/edit/etc.
API_POOL_SIZE: int = 8
