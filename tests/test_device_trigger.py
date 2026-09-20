"""USPS device trigger contract tests."""
from unittest.mock import AsyncMock

from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.helpers.trigger import TriggerInfo

from custom_components.usps.const import DOMAIN
from custom_components.usps.device_trigger import (
    TRIGGER_EVENTS,
    async_attach_trigger,
    async_get_triggers,
)


async def test_device_trigger_list_matches_published_events(hass):
    triggers = await async_get_triggers(hass, "device-id")
    assert {trigger[CONF_TYPE] for trigger in triggers} == set(TRIGGER_EVENTS)
    assert all(trigger[CONF_PLATFORM] == "device" and trigger[CONF_DOMAIN] == DOMAIN and trigger[CONF_DEVICE_ID] == "device-id" for trigger in triggers)


async def test_attached_trigger_fires_on_this_device_s_event(hass):
    """The device trigger delegates to the generic event trigger, filtered on
    the device_id the coordinator attaches to every event — attaching it and
    firing the event must call through.
    """
    action = AsyncMock()
    remove = await async_attach_trigger(
        hass,
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: "device-id",
            CONF_TYPE: "parcel_delivered",
        },
        action,
        TriggerInfo(trigger_data={"id": "0", "idx": "0"}, variables={}),
    )

    hass.bus.async_fire(
        TRIGGER_EVENTS["parcel_delivered"],
        {CONF_DEVICE_ID: "other-device", "barcode": "9400"},
    )
    await hass.async_block_till_done()
    action.assert_not_called()

    hass.bus.async_fire(
        TRIGGER_EVENTS["parcel_delivered"],
        {CONF_DEVICE_ID: "device-id", "barcode": "9400"},
    )
    await hass.async_block_till_done()
    action.assert_called_once()

    remove()
