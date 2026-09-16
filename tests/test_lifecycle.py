"""Multi-entry lifecycle regression tests."""
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.usps import async_unload_entry
from custom_components.usps.const import (
    CONF_SOURCE,
    DOMAIN,
    SOURCE_API_TRACKING,
    SOURCE_INFORMED_DELIVERY,
)
from custom_components.usps.services import async_setup_services


async def test_unloading_informed_delivery_keeps_services_for_api_tracking(hass):
    api = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_API_TRACKING})
    inbox = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_INFORMED_DELIVERY})
    api.add_to_hass(hass)
    inbox.add_to_hass(hass)
    async_setup_services(hass)
    with patch.object(hass.config_entries, "async_unload_platforms", new=AsyncMock(return_value=True)):
        assert await async_unload_entry(hass, inbox)
    assert hass.services.has_service(DOMAIN, "track_parcel")
