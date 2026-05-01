"""Clerk JWT token manager with auto-refresh.

The Higgsfield web app authenticates with a short-lived JWT (~5 min TTL)
issued by Clerk. We refresh proactively every 4 minutes using the long-lived
__client cookie + session ID extracted from the user's browser.

HTTP transport is curl_cffi (impersonating Chrome) to defeat Cloudflare
bot fingerprinting on fnf.higgsfield.ai.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from curl_cffi.requests import AsyncSession

from .errors import AuthError

log = logging.getLogger(__name__)

CLERK_TOKEN_URL = (
    "https://clerk.higgsfield.ai/v1/client/sessions/{session_id}/tokens"
    "?debug=skip_cache&__clerk_api_version=2025-11-10&_clerk_js_version=5.125.10"
)
TOKEN_REFRESH_SECONDS = 240  # JWT TTL is ~5 min; refresh at 4 min


class TokenManager:
    """Thread-safe (asyncio) Clerk JWT manager with proactive refresh."""

    def __init__(self, session_id: str, client_cookie: str):
        self._session_id = session_id
        self._client_cookie = client_cookie
        self._token: Optional[str] = None
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self, http: AsyncSession, force: bool = False) -> str:
        """Return a valid JWT, refreshing if it's been > 4 min."""
        async with self._lock:
            now = time.time()
            if force or self._token is None or (now - self._fetched_at) > TOKEN_REFRESH_SECONDS:
                self._token = await self._fetch(http)
                self._fetched_at = now
                log.debug("Refreshed Clerk JWT (session=%s)", self._session_id[:12])
            return self._token

    async def invalidate(self) -> None:
        """Force next call to refetch (e.g. after a 401 response)."""
        async with self._lock:
            self._token = None
            self._fetched_at = 0.0

    async def _fetch(self, http: AsyncSession) -> str:
        url = CLERK_TOKEN_URL.format(session_id=self._session_id)
        try:
            resp = await http.post(
                url,
                data={"organization_id": ""},
                headers={
                    "Cookie": f"__client={self._client_cookie}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://higgsfield.ai",
                    "Referer": "https://higgsfield.ai/",
                },
            )
        except Exception as e:
            raise AuthError(f"Network error talking to Clerk: {e}") from e

        if resp.status_code in (401, 403):
            raise AuthError(
                f"Clerk auth rejected (HTTP {resp.status_code}). Your __client cookie or "
                f"session ID is invalid or expired. Re-extract from Chrome DevTools. "
                f"Body: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise AuthError(f"Clerk request failed (HTTP {resp.status_code}): {resp.text[:200]}")

        data = resp.json()
        token = data.get("jwt")
        if not token:
            raise AuthError(f"No 'jwt' field in Clerk response: {data}")
        return token
