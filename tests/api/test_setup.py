"""API Tracking setup and shared platform smoke tests."""
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.usps.const import (
    CONF_CONSUMER_KEY,
    CONF_CONSUMER_SECRET,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_API_TRACKING,
)


async def test_api_tracking_setup_exposes_shared_platforms(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_API_TRACKING, CONF_CONSUMER_KEY: "key", CONF_CONSUMER_SECRET: "secret"}, options={CONF_PARCELS: [{CONF_TRACKING_CODE: "9400"}]})
    entry.add_to_hass(hass)
    raw = {"trackingNumber": "9400", "statusCategory": "Accepted", "status": "USPS in possession of item", "trackingEvents": []}
    with patch("custom_components.usps.api.client.ApiTrackingClient.async_get_parcel", new=AsyncMock(return_value=raw)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.usps_incoming_parcels").state == "1"
    assert hass.states.get("button.usps_refresh") is not None
    assert hass.services.has_service(DOMAIN, "track_parcel")
