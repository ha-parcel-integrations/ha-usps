"""Read-only Informed Delivery inbox client.

Authentication is intentionally kept separate from API Tracking: this client
never sees Business API credentials or its bearer token.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from ..api_tracking.client import USPSApiError, USPSAuthError
from ..const import INFORMED_DELIVERY_API_URL
from .auth import exchange_refresh_token

EASTERN = ZoneInfo("America/New_York")
_LOGGER = logging.getLogger(__name__)


class InformedDeliveryTokenExpired(USPSAuthError):
    """A 401 USPS explicitly identifies as an access-token expiry."""


class InformedDeliveryClient:
    """Fetch an already-authenticated household package inbox.

    The credential journey is deliberately injected by the config flow. This
    makes no claims about undocumented callback shapes beyond its safe caller.
    """

    def __init__(self, access_token: str, refresh_token: str, session: aiohttp.ClientSession) -> None:
        """Initialise a client scoped to this household session."""
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._refresh_generation = 0
        self._session = session

    @property
    def refresh_token(self) -> str:
        """Return the latest rotated refresh token for atomic persistence."""
        return self._refresh_token

    @property
    def refresh_generation(self) -> int:
        """Monotonic counter used to avoid duplicate concurrent refreshes."""
        return self._refresh_generation

    async def async_refresh(self) -> None:
        """Refresh the short-lived access token and retain the rotated chain."""
        _LOGGER.debug("USPS Informed Delivery: refreshing access token (generation %s)", self._refresh_generation)
        tokens = await exchange_refresh_token(self._session, self._refresh_token)
        self._access_token = tokens["access_token"]
        self._refresh_token = tokens["refresh_token"]
        self._refresh_generation += 1
        _LOGGER.debug("USPS Informed Delivery: access token refreshed (generation %s)", self._refresh_generation)

    async def async_get_packages(self, zip11: str) -> list[dict[str, Any]]:
        """Return inbound and manually added packages for one address."""
        # Never log the raw ZIP11 (it is a precise home address) — a stable
        # hash is enough to tell "which address" apart in a multi-address log.
        address_id = hashlib.sha256(zip11.encode()).hexdigest()[:8]
        date = datetime.now(EASTERN).date().isoformat()
        headers = {"Authorization": f"Bearer {self._access_token}", "Origin": "https://informeddelivery.usps.com"}
        body = {"ZIP11": zip11, "deliveryDate": date, "source": "CP", "electronicSignatureEligible": False, "maxInboundPackages": 100, "maxAddedPackages": 100, "campaigns": True}
        _LOGGER.debug("USPS Informed Delivery: POST packages/search address=%s date=%s", address_id, date)
        async with self._session.post(INFORMED_DELIVERY_API_URL, json=body, headers=headers) as response:
            _LOGGER.debug(
                "USPS Informed Delivery: packages/search address=%s -> HTTP %s",
                address_id,
                response.status,
            )
            if response.status == 401:
                try:
                    error = await response.json(content_type=None)
                except ValueError:
                    error = {}
                code = str(error.get("code", "")) if isinstance(error, dict) else ""
                if code in {"", "401", "TOKEN_EXPIRED", "EXPIRED_TOKEN"}:
                    _LOGGER.debug("USPS Informed Delivery: access token expired (code=%s), will refresh once", code)
                    raise InformedDeliveryTokenExpired("Informed Delivery access token expired", status_code=401)
                _LOGGER.warning("USPS Informed Delivery: session was revoked (401 code=%s) — reauth required", code)
                raise USPSAuthError("Informed Delivery session was revoked", status_code=401)
            if response.status == 204:
                _LOGGER.debug("USPS Informed Delivery: address=%s inbox is empty (204)", address_id)
                return []
            if response.status == 429:
                try:
                    retry_after = float(response.headers.get("Retry-After", ""))
                except ValueError:
                    retry_after = None
                _LOGGER.warning("USPS Informed Delivery: rate limited (429, retry_after=%s)", retry_after)
                raise USPSApiError("HTTP 429", status_code=429, retry_after=retry_after)
            if response.status != 200:
                raise USPSApiError(f"HTTP {response.status}", status_code=response.status)
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise USPSApiError("unparseable package inbox") from err
        if not isinstance(payload, dict):
            raise USPSApiError("unexpected package inbox body")
        packages: list[dict[str, Any]] = []
        for key, user_added in (("inboundPackages", False), ("addedPackages", True)):
            records = payload.get(key, []) or []
            if len(records) == 100:
                _LOGGER.warning("USPS Informed Delivery inbox may be truncated at the 100-item %s limit", key)
            for item in records:
                if isinstance(item, dict):
                    packages.append({**item, "_user_added": user_added, "_address_id": zip11})
        _LOGGER.debug("USPS Informed Delivery: address=%s returned %s package record(s)", address_id, len(packages))
        return packages
