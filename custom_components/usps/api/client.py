"""USPS Business Tracking API client (the ``api_tracking`` source)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from ..const import TOKEN_API_URL, TRACKING_API_URL

_LOGGER = logging.getLogger(__name__)


class USPSApiError(Exception):
    """A non-authentication USPS API failure."""

    def __init__(self, detail: str, *, status_code: int | None = None, retry_after: float | None = None) -> None:
        """Store response metadata used by coordinator backoff."""
        super().__init__(f"USPS API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class USPSAuthError(USPSApiError):
    """USPS rejected the configured API Tracking credentials."""


class TrackingNotEnabledError(USPSAuthError):
    """The credentials work but were not issued the Tracking scope."""


class ApiTrackingClient:
    """OAuth client-credentials client with an in-memory access-token cache."""

    def __init__(self, consumer_key: str, consumer_secret: str, session: aiohttp.ClientSession) -> None:
        """Initialise the client without persisting an access token."""
        self._consumer_key = consumer_key
        self._consumer_secret = consumer_secret
        self._session = session
        self._access_token: str | None = None
        self._expires_at: datetime | None = None

    async def _async_token(self, *, force: bool = False) -> str:
        now = datetime.now(timezone.utc)
        if not force and self._access_token and self._expires_at and now < self._expires_at:
            return self._access_token
        body = {"client_id": self._consumer_key, "client_secret": self._consumer_secret, "grant_type": "client_credentials"}
        _LOGGER.debug("USPS API Tracking: POST /oauth2/v3/token (force=%s)", force)
        async with self._session.post(TOKEN_API_URL, json=body) as response:
            _LOGGER.debug("USPS API Tracking: /oauth2/v3/token -> HTTP %s", response.status)
            if response.status in (400, 401, 403):
                raise USPSAuthError(f"HTTP {response.status}", status_code=response.status)
            if response.status != 200:
                raise USPSApiError(f"HTTP {response.status}", status_code=response.status)
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise USPSApiError("unparseable token response") from err
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise USPSAuthError("token response did not contain an access token")
        scope = payload.get("scope", "")
        if isinstance(scope, str) and "tracking" not in scope.lower().split():
            _LOGGER.debug("USPS API Tracking: token issued but scope %r lacks 'tracking'", scope)
            raise TrackingNotEnabledError("Tracking scope is not enabled")
        try:
            lifetime = max(0, int(payload.get("expires_in", 0)) - 30)
        except (TypeError, ValueError):
            lifetime = 0
        self._access_token = token
        self._expires_at = now + timedelta(seconds=lifetime)
        _LOGGER.debug("USPS API Tracking: access token cached for %ss", lifetime)
        return token

    async def async_validate_credentials(self) -> None:
        """Validate credentials and the required Tracking entitlement."""
        await self._async_token(force=True)

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one v3 detail payload, refreshing once after a 401."""
        for attempt in range(2):
            token = await self._async_token(force=attempt > 0)
            url = TRACKING_API_URL.format(tracking_code=tracking_code)
            async with self._session.get(url, params={"expand": "DETAIL"}, headers={"Authorization": f"Bearer {token}"}) as response:
                _LOGGER.debug(
                    "USPS API Tracking: GET tracking/v3 code=%s attempt=%s -> HTTP %s",
                    tracking_code,
                    attempt,
                    response.status,
                )
                if response.status == 401 and attempt == 0:
                    self._access_token = None
                    continue
                if response.status in (401, 403):
                    raise USPSAuthError(f"HTTP {response.status}", status_code=response.status)
                if response.status == 404:
                    return None
                if response.status == 429:
                    try:
                        retry_after = float(response.headers.get("Retry-After", ""))
                    except ValueError:
                        retry_after = None
                    _LOGGER.warning("USPS API Tracking: rate limited (429, retry_after=%s)", retry_after)
                    raise USPSApiError("HTTP 429", status_code=429, retry_after=retry_after)
                if response.status != 200:
                    raise USPSApiError(f"HTTP {response.status}", status_code=response.status)
                try:
                    payload = await response.json(content_type=None)
                except ValueError as err:
                    raise USPSApiError("unparseable tracking response") from err
            if not isinstance(payload, dict):
                raise USPSApiError("unexpected body (not a JSON object)")
            return payload
        raise USPSAuthError("token refresh failed")
