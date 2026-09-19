"""End-to-end Home Assistant service tests for API Tracking only."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.usps.const import (
    CONF_CONSUMER_KEY,
    CONF_CONSUMER_SECRET,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_API_TRACKING,
    SOURCE_INFORMED_DELIVERY,
)


async def _setup_api(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_API_TRACKING, CONF_CONSUMER_KEY: "key", CONF_CONSUMER_SECRET: "secret"}, options={CONF_PARCELS: []})
    entry.add_to_hass(hass)
    with patch("custom_components.usps.api.client.ApiTrackingClient.async_get_parcel", new=AsyncMock(return_value=None)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_track_service_mutates_only_target_api_entry(hass):
    entry = await _setup_api(hass)
    await hass.services.async_call(DOMAIN, "track_parcel", {CONF_TRACKING_CODE: "9400", "entry_id": entry.entry_id}, blocking=True)
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: "9400"}]


async def test_track_service_rejects_informed_delivery_entry(hass):
    await _setup_api(hass)
    inbox = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_INFORMED_DELIVERY})
    inbox.add_to_hass(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "track_parcel", {CONF_TRACKING_CODE: "9400", "entry_id": inbox.entry_id}, blocking=True)


async def test_track_duplicate_is_noop_and_untrack_removes_code(hass):
    entry = await _setup_api(hass)
    payload = {CONF_TRACKING_CODE: "9400", "entry_id": entry.entry_id}
    await hass.services.async_call(DOMAIN, "track_parcel", payload, blocking=True)
    await hass.services.async_call(DOMAIN, "track_parcel", payload, blocking=True)
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: "9400"}]
    await hass.services.async_call(DOMAIN, "untrack_parcel", payload, blocking=True)
    assert entry.options[CONF_PARCELS] == []
