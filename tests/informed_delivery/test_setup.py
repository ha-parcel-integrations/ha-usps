"""Informed Delivery setup smoke test for shared HA platforms."""
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.usps.const import (
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    DOMAIN,
    SOURCE_INFORMED_DELIVERY,
)


async def test_informed_delivery_setup_exposes_inbox_entities(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_INFORMED_DELIVERY, "access_token": "access", CONF_REFRESH_TOKEN: "refresh", "addresses": [{"zip11": "12345678901"}]})
    entry.add_to_hass(hass)
    package = {"trackingNumber": "9400", "deliveryInfo": {"statusCategory": "Out for Delivery", "deliveryDate": "2026-09-16"}}
    with patch("custom_components.usps.informed_delivery.client.InformedDeliveryClient.async_get_packages", new=AsyncMock(return_value=[package])):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.usps_incoming_parcels").state == "1"
    assert hass.states.get("calendar.usps_deliveries") is not None
    assert hass.states.get("button.usps_refresh") is not None
