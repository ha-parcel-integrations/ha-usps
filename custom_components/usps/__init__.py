"""USPS parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account.client import InformedDeliveryClient
from .account.coordinator import InformedDeliveryCoordinator
from .account.session import async_new_informed_delivery_session
from .api.client import ApiTrackingClient
from .api.coordinator import USPSCoordinator
from .const import (
    CONF_CONSUMER_KEY,
    CONF_CONSUMER_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    PLATFORMS,
    SOURCE_API_TRACKING,
    SOURCE_INFORMED_DELIVERY,
)
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


@dataclass
class USPSData:
    """Runtime data attached to the USPS config entry."""

    client: object
    coordinator: object


type USPSConfigEntry = ConfigEntry[USPSData]


async def async_setup_entry(hass: HomeAssistant, entry: USPSConfigEntry) -> bool:
    """Set up USPS from a config entry.

    The key is stateless (no cookie/session to keep separate per entry, unlike
    the account-login variant), so the HA-managed session is fine and there is
    no login step here — a rejected key surfaces from the first coordinator
    refresh below as ``ConfigEntryAuthFailed``, which HA turns into a reauth
    prompt on its own.
    """
    if entry.data.get(CONF_SOURCE) == SOURCE_API_TRACKING:
        client = ApiTrackingClient(entry.data[CONF_CONSUMER_KEY], entry.data[CONF_CONSUMER_SECRET], async_get_clientsession(hass))
        coordinator = USPSCoordinator(hass, client, entry)
    elif entry.data.get(CONF_SOURCE) == SOURCE_INFORMED_DELIVERY:
        # Never HA's shared session — the Akamai edge in front of
        # verified.usps.com is behind a bot-management edge that rejects
        # requests on HA's shared connector — see session.py. This session
        # owns a real, separate TCPConnector (not merely a separate
        # ClientSession sharing HA's pooled one), so it is ours to close.
        session = async_new_informed_delivery_session(hass)
        entry.async_on_unload(session.close)
        client = InformedDeliveryClient(entry.data["access_token"], entry.data[CONF_REFRESH_TOKEN], session)
        coordinator = InformedDeliveryCoordinator(hass, client, entry)
    else:
        raise ValueError("USPS entry has no supported source")

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady from a forwarded platform is too late for HA to catch
    # cleanly (it logs a warning and half-sets-up the entry); doing the first
    # refresh here lets a transient failure fail the whole entry so HA retries
    # it with backoff.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = USPSData(client=client, coordinator=coordinator)

    if entry.data.get(CONF_SOURCE) == SOURCE_INFORMED_DELIVERY:
        entry.async_on_unload(coordinator.async_start_keepalive())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Apply option changes (added/removed parcels, history) live via a
    # coordinator refresh — no reload — so per-parcel sensors appear and
    # disappear immediately. The update listener does NOT reload, so it does
    # not trip the config-entry-listener deprecation. This is also the resume
    # path after polling fully suspended (Section 2.1): adding a parcel back
    # triggers this refresh, which recomputes the tier and re-arms scheduling.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    async_setup_services(hass)

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: USPSConfigEntry
) -> None:
    """Apply changed options by refreshing the coordinator."""
    await entry.runtime_data.coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: USPSConfigEntry) -> bool:
    """Unload the USPS config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    if not any(other.data.get(CONF_SOURCE) == SOURCE_API_TRACKING for other in hass.config_entries.async_entries("usps") if other.entry_id != entry.entry_id):
        async_unload_services(hass)
    return True
