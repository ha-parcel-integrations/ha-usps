"""USPS device trigger contract tests."""
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE

from custom_components.usps.const import DOMAIN
from custom_components.usps.device_trigger import TRIGGER_EVENTS, async_get_triggers


async def test_device_trigger_list_matches_published_events(hass):
    triggers = await async_get_triggers(hass, "device-id")
    assert {trigger[CONF_TYPE] for trigger in triggers} == set(TRIGGER_EVENTS)
    assert all(trigger[CONF_PLATFORM] == "device" and trigger[CONF_DOMAIN] == DOMAIN and trigger[CONF_DEVICE_ID] == "device-id" for trigger in triggers)
