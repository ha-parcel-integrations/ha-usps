"""Coordinator for the address-inbox Informed Delivery source."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import logging
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ..api.client import USPSApiError, USPSAuthError
from ..const import CONF_REFRESH_TOKEN, DOMAIN, ParcelStatus
from .client import InformedDeliveryClient, InformedDeliveryTokenExpired
from .letters import extract_mail_pieces, merge_mail_pieces
from .parcels import normalize_informed_delivery_parcel

_LOGGER = logging.getLogger(__name__)

# Only the mail/packages request date is US Eastern; everything in the payload
# is local to the enrolled address.
EASTERN = ZoneInfo("America/New_York")


class InformedDeliveryCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    """Poll all enrolled addresses while preserving separate source data."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, client: InformedDeliveryClient, entry: ConfigEntry
    ) -> None:
        """Initialise a fixed-cadence, source-isolated coordinator."""
        # USPS rotates its refresh token roughly every 15 minutes.  Polling at
        # ten minutes keeps the chain alive while HA is running; a longer
        # interval can silently turn an otherwise healthy entry into reauth.
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(minutes=10),
        )
        self._client = client
        self.delivered: list[dict[str, Any]] = []
        self._current_tier_minutes = 10
        self._delivered_codes: set[str] = set()
        self._refresh_lock = asyncio.Lock()
        self._last_good: list[dict[str, Any]] = []
        # ``None`` until the first refresh so a restart does not flood users
        # with "registered" events for parcels that were already there.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: dict[str, tuple[str | None, str | None]] | None = (
            None
        )
        self._cached_device_id: str | None = None
        self.last_success_time: datetime | None = None
        self.letters: list[dict[str, Any]] = []
        # ``None`` until the first successful mail fetch, so a restart does not
        # announce every letter that was already in the dashboard.
        self._known_letter_ids: set[str] | None = None

    @property
    def current_tier_minutes(self) -> int:
        """Return the fixed inbox polling cadence."""
        return self._current_tier_minutes

    @property
    def delivered_codes(self) -> set[str]:
        """Return the delivered codes for diagnostics compatibility."""
        return self._delivered_codes

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    def _fire_change_events(self, parcels: list[dict[str, Any]]) -> None:
        """Fire registered / status-changed / delivered / delivery-time events.

        Same contract as the API Tracking coordinator: silent on the first
        refresh, the hop to ``delivered`` fires only ``_parcel_delivered``, a
        barcode first seen already delivered fires nothing, and an ETA going
        ``value → null`` stays silent.
        """
        if self._known_state is None:
            return

        known_times = self._known_delivery_times or {}
        device_id = self._device_id()

        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode:
                continue
            new_status = parcel["status"]
            if barcode not in self._known_state:
                if new_status != ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_registered",
                        {**parcel, "device_id": device_id},
                    )
                continue

            if self._known_state[barcode] != new_status:
                if new_status == ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_delivered",
                        {**parcel, "device_id": device_id},
                    )
                else:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_status_changed",
                        {
                            **parcel,
                            "device_id": device_id,
                            "old_status": self._known_state[barcode],
                            "new_status": new_status,
                        },
                    )

            old_from, old_to = known_times.get(barcode, (None, None))
            new_from = parcel.get("planned_from")
            new_to = parcel.get("planned_to")
            if (new_from is not None and new_from != old_from) or (
                new_to is not None and new_to != old_to
            ):
                self.hass.bus.async_fire(
                    f"{DOMAIN}_parcel_delivery_time_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_planned_from": old_from,
                        "new_planned_from": new_from,
                        "old_planned_to": old_to,
                        "new_planned_to": new_to,
                    },
                )

    def _persist_tokens(self) -> None:
        """Write the current token pair back to the entry.

        Both halves matter: the access token stored at login is dead five
        minutes later, so an entry that only ever persisted the refresh token
        came back from a restart with a token USPS was certain to reject.
        """
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                "access_token": self._client.access_token,
                CONF_REFRESH_TOKEN: self._client.refresh_token,
            },
        )

    async def async_restore_session(self) -> None:
        """Renew the stored chain before the first poll after a restart.

        Starting that poll on the stored access token would make every
        restart depend on USPS naming the expiry in its 401 body; a chain
        that is genuinely dead still surfaces here as reauth.
        """
        try:
            async with self._refresh_lock:
                await self._client.async_refresh()
                self._persist_tokens()
        except USPSAuthError as err:
            raise ConfigEntryAuthFailed(
                "Informed Delivery requires reauthentication"
            ) from err
        except (USPSApiError, aiohttp.ClientError, TimeoutError) as err:
            raise ConfigEntryNotReady(str(err)) from err

    def async_start_keepalive(self) -> Callable[[], None]:
        """Refresh the rotating token chain independently of inbox polling."""

        async def _keepalive(_now: datetime) -> None:
            self.logger.debug("USPS Informed Delivery: keepalive refresh firing")
            try:
                async with self._refresh_lock:
                    await self._client.async_refresh()
                    self._persist_tokens()
            except USPSAuthError:
                self.logger.warning(
                    "USPS Informed Delivery token refresh failed — starting reauth"
                )
                self.config_entry.async_start_reauth(self.hass)

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
                    self._persist_tokens()
                rows = []
                for address in addresses:
                    rows.extend(await self._client.async_get_packages(address["zip11"]))
            except USPSAuthError as refresh_err:
                raise ConfigEntryAuthFailed(
                    "Informed Delivery requires reauthentication"
                ) from refresh_err
        except USPSAuthError as err:
            raise ConfigEntryAuthFailed(
                "Informed Delivery requires reauthentication"
            ) from err
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
            parcels.append(
                normalize_informed_delivery_parcel(
                    row, tz=dt_util.get_default_time_zone()
                )
            )
        self.delivered = [parcel for parcel in parcels if parcel["delivered"]]
        self._last_good = [parcel for parcel in parcels if not parcel["delivered"]]
        self._fire_change_events(self._last_good + self.delivered)
        self._known_state = {
            parcel["barcode"]: parcel["status"]
            for parcel in parcels
            if parcel.get("barcode")
        }
        self._known_delivery_times = {
            parcel["barcode"]: (parcel.get("planned_from"), parcel.get("planned_to"))
            for parcel in parcels
            if parcel.get("barcode")
        }
        await self._async_update_letters(addresses)
        self.last_success_time = datetime.now(UTC)
        _LOGGER.debug(
            "USPS Informed Delivery: poll returned %s active, %s delivered "
            "parcel(s), %s letter(s)",
            len(self._last_good),
            len(self.delivered),
            len(self.letters),
        )
        return self._last_good

    async def _async_update_letters(self, addresses: list[dict[str, Any]]) -> None:
        """Refresh the scanned letter-mail list for every enrolled address.

        Never fatal: the parcel inbox is this integration's job and mail rides
        along with it, so a mail failure logs and leaves the previous list in
        place rather than failing a poll that already has its parcels. Today
        and yesterday are both fetched and unioned — before the morning batch
        posts, "no mail yet today" and "no mail today" look identical.
        """
        today = datetime.now(EASTERN).date()
        batches: list[list[dict[str, Any]]] = []
        for address in addresses:
            for offset in (0, 1):
                date = (today - timedelta(days=offset)).isoformat()
                try:
                    payload = await self._client.async_get_mail_pieces(
                        address["zip11"], date
                    )
                except (USPSApiError, aiohttp.ClientError, TimeoutError) as err:
                    _LOGGER.warning(
                        "USPS Informed Delivery: could not fetch scanned mail for "
                        "%s — keeping the previous list (%s)",
                        date,
                        err,
                    )
                    return
                batches.append(extract_mail_pieces(payload, requested_date=date))
        self.letters = merge_mail_pieces(batches)
        self._fire_letter_events(self.letters)
        self._known_letter_ids = {letter["id"] for letter in self.letters}

    async def async_get_letter_image(self, href: str) -> bytes | None:
        """Fetch one scan, refreshing the access token once if it expired.

        An image entity can be rendered at any point between polls, long after
        the five-minute access token the last poll used has died, so the 401
        retry lives here rather than being left to the next poll.
        """
        try:
            return await self._client.async_get_image(href)
        except InformedDeliveryTokenExpired:
            generation = self._client.refresh_generation
            async with self._refresh_lock:
                if self._client.refresh_generation == generation:
                    await self._client.async_refresh()
                self._persist_tokens()
            return await self._client.async_get_image(href)

    def _fire_letter_events(self, letters: list[dict[str, Any]]) -> None:
        """Fire ``usps_letter_announced`` for letter ids not seen before.

        Silent on the first successful mail fetch: every letter is "new" then,
        and announcing a dashboard's worth of post at startup is noise.
        """
        if self._known_letter_ids is None:
            return
        device_id = self._device_id()
        for letter in letters:
            if letter["id"] in self._known_letter_ids:
                continue
            self.hass.bus.async_fire(
                f"{DOMAIN}_letter_announced",
                {**letter, "carrier": "USPS", "device_id": device_id},
            )
