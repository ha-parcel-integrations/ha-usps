"""Coordinator for the address-inbox Informed Delivery source."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..api_tracking.client import USPSApiError, USPSAuthError
from ..const import DOMAIN
from .client import InformedDeliveryClient, InformedDeliveryTokenExpired
from .parcels import normalize_informed_delivery_parcel

_LOGGER = logging.getLogger(__name__)


class InformedDeliveryCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    """Poll all enrolled addresses while preserving separate source data."""

    def __init__(self, hass: HomeAssistant, client: InformedDeliveryClient, entry: ConfigEntry) -> None:
        """Initialise a fixed-cadence, source-isolated coordinator."""
        # USPS rotates its refresh token roughly every 15 minutes.  Polling at
        # ten minutes keeps the chain alive while HA is running; a longer
        # interval can silently turn an otherwise healthy entry into reauth.
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=timedelta(minutes=10))
        self._client = client
        self.delivered: list[dict[str, Any]] = []
        self._current_tier_minutes = 10
        self._delivered_codes: set[str] = set()
        self._refresh_lock = asyncio.Lock()
        self._last_good: list[dict[str, Any]] = []
        self.last_success_time = None

    @property
    def current_tier_minutes(self) -> int:
        """Return the fixed inbox polling cadence."""
        return self._current_tier_minutes

    @property
    def delivered_codes(self) -> set[str]:
        """Return the delivered codes for diagnostics compatibility."""
        return self._delivered_codes

    def async_start_keepalive(self) -> Callable[[], None]:
        """Refresh the rotating token chain independently of inbox polling."""
        async def _keepalive(_now: datetime) -> None:
            self.logger.debug("USPS Informed Delivery: keepalive refresh firing")
            try:
                async with self._refresh_lock:
                    await self._client.async_refresh()
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        data={
                            **self.config_entry.data,
                            "refresh_token": self._client.refresh_token,
                        },
                    )
            except USPSAuthError:
                self.logger.warning("USPS Informed Delivery token refresh failed — starting reauth")
                self.hass.async_create_task(
                    self.hass.config_entries.flow.async_init(
                        DOMAIN,
                        context={"source": "reauth", "entry_id": self.config_entry.entry_id},
                        data=self.config_entry.data,
                    )
                )

        return async_track_time_interval(self.hass, _keepalive, timedelta(minutes=10))

    async def _async_update_data(self) -> list[dict[str, Any]]:
        addresses = self.config_entry.data.get("addresses", [])
        _LOGGER.debug("USPS Informed Delivery: polling %s address(es)", len(addresses))
        try:
            rows: list[dict[str, Any]] = []
            for address in addresses:
                rows.extend(await self._client.async_get_packages(address["zip11"]))
        except InformedDeliveryTokenExpired:
            # A data 401 may be only the five-minute access token expiring.
            # Refresh exactly once under a lock; a 400 refresh failure is a
            # dead rotated chain and correctly becomes interactive reauth.
            try:
                generation = self._client.refresh_generation
                async with self._refresh_lock:
                    if self._client.refresh_generation == generation:
                        await self._client.async_refresh()
                    self.hass.config_entries.async_update_entry(self.config_entry, data={**self.config_entry.data, "refresh_token": self._client.refresh_token})
                rows = []
                for address in addresses:
                    rows.extend(await self._client.async_get_packages(address["zip11"]))
            except USPSAuthError as refresh_err:
                raise ConfigEntryAuthFailed("Informed Delivery requires reauthentication") from refresh_err
        except USPSAuthError as err:
            raise ConfigEntryAuthFailed("Informed Delivery requires reauthentication") from err
        except USPSApiError as err:
            if self._last_good:
                return self._last_good
            raise UpdateFailed(str(err)) from err
        seen: set[str] = set()
        first_by_code: dict[str, dict[str, Any]] = {}
        parcels: list[dict[str, Any]] = []
        for row in rows:
            code = row.get("trackingNumber")
            if code and code in seen:
                first_by_code[code].setdefault("_duplicate_sources", []).append(
                    "addedPackages" if row.get("_user_added") else "inboundPackages"
                )
                continue
            if code:
                seen.add(code)
                first_by_code[code] = row
            parcels.append(normalize_informed_delivery_parcel(row))
        self.delivered = [parcel for parcel in parcels if parcel["delivered"]]
        self._last_good = [parcel for parcel in parcels if not parcel["delivered"]]
        _LOGGER.debug(
            "USPS Informed Delivery: poll returned %s active, %s delivered parcel(s)",
            len(self._last_good),
            len(self.delivered),
        )
        return self._last_good
