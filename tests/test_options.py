"""Source-aware options flow tests."""
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.usps.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_API_TRACKING,
    SOURCE_INFORMED_DELIVERY,
)


async def test_informed_delivery_options_only_offer_settings(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_INFORMED_DELIVERY})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == ["settings"]


async def test_api_tracking_options_offer_parcels_and_settings(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_API_TRACKING})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == ["parcels", "settings"]


async def test_parcels_step_normalises_and_dedupes_codes(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_API_TRACKING}, options={CONF_PARCELS: []})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "parcels"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["94-00 aa", "9400AA"]}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: "9400AA"}]


async def test_settings_step_updates_options_and_keeps_parcels(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SOURCE: SOURCE_API_TRACKING},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: "9400AA"}]},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DELIVERED_FILTER_TYPE: "parcels", CONF_DELIVERED_FILTER_AMOUNT: 3, CONF_INCLUDE_HISTORY: True},
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: "9400AA"}]
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 3
    assert result["data"][CONF_INCLUDE_HISTORY] is True


async def test_informed_delivery_settings_step_updates_options(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_INFORMED_DELIVERY}, options={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DELIVERED_FILTER_TYPE: "days", CONF_DELIVERED_FILTER_AMOUNT: 5, CONF_INCLUDE_HISTORY: False},
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 5
