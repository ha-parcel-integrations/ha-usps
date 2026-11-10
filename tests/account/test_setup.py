"""Informed Delivery setup smoke test for shared HA platforms."""

from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.usps.api.client import USPSAuthError
from custom_components.usps.const import (
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    DOMAIN,
    SOURCE_INFORMED_DELIVERY,
)


async def test_informed_delivery_setup_exposes_inbox_entities(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE: SOURCE_INFORMED_DELIVERY,
            "access_token": "access",
            CONF_REFRESH_TOKEN: "refresh",
            "addresses": [{"zip11": "12345678901"}],
        },
    )
    entry.add_to_hass(hass)
    package = {
        "trackingNumber": "9400",
        "deliveryInfo": {
            "statusCategory": "Out for Delivery",
            "deliveryDate": "2026-09-16",
        },
    }
    with (
        patch(
            "custom_components.usps.account.client.exchange_refresh_token",
            new=AsyncMock(
                return_value={"access_token": "fresh", "refresh_token": "rotated"}
            ),
        ),
        patch(
            "custom_components.usps.account.client.InformedDeliveryClient.async_get_mail_pieces",
            new=AsyncMock(return_value={"mailpieces": []}),
        ),
        patch(
            "custom_components.usps.account.client.InformedDeliveryClient.async_get_packages",
            new=AsyncMock(return_value=[package]),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.usps_incoming_parcels").state == "1"
    assert hass.states.get("calendar.usps_deliveries") is not None
    assert hass.states.get("button.usps_refresh") is not None


async def test_setup_renews_the_token_chain_before_the_first_poll(hass):
    """A restart must not poll on the access token stored at login."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE: SOURCE_INFORMED_DELIVERY,
            "access_token": "stale",
            CONF_REFRESH_TOKEN: "refresh",
            "addresses": [{"zip11": "12345678901"}],
        },
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.usps.account.client.exchange_refresh_token",
            new=AsyncMock(
                return_value={"access_token": "fresh", "refresh_token": "rotated"}
            ),
        ) as refresh,
        patch(
            "custom_components.usps.account.client.InformedDeliveryClient.async_get_mail_pieces",
            new=AsyncMock(return_value={"mailpieces": []}),
        ),
        patch(
            "custom_components.usps.account.client.InformedDeliveryClient.async_get_packages",
            new=AsyncMock(return_value=[]),
        ) as packages,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert refresh.await_args.args[1] == "refresh"
        assert packages.await_count == 1
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data["access_token"] == "fresh"
    assert entry.data[CONF_REFRESH_TOKEN] == "rotated"


async def test_setup_on_a_dead_chain_asks_for_reauthentication(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE: SOURCE_INFORMED_DELIVERY,
            "access_token": "stale",
            CONF_REFRESH_TOKEN: "refresh",
            "addresses": [{"zip11": "12345678901"}],
        },
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.usps.account.client.exchange_refresh_token",
        new=AsyncMock(side_effect=USPSAuthError("dead chain", status_code=400)),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_setup_retries_when_the_renewal_cannot_reach_usps(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE: SOURCE_INFORMED_DELIVERY,
            "access_token": "stale",
            CONF_REFRESH_TOKEN: "refresh",
            "addresses": [{"zip11": "12345678901"}],
        },
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.usps.account.client.exchange_refresh_token",
        new=AsyncMock(side_effect=aiohttp.ClientError("boom")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_token_rotation_does_not_count_as_an_options_edit(hass):
    """A keepalive token write must not poll — the user may have polling off.

    Every ``async_update_entry`` notifies the entry's update listener, and the
    keepalive rotates tokens onto the entry every ten minutes. Refreshing on
    that notification polled USPS through ``async_request_refresh``, which
    ignores Home Assistant's "Enable polling for updates" system option.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE: SOURCE_INFORMED_DELIVERY,
            "access_token": "access",
            CONF_REFRESH_TOKEN: "refresh",
            "addresses": [{"zip11": "12345678901"}],
        },
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.usps.account.client.exchange_refresh_token",
            new=AsyncMock(
                return_value={"access_token": "fresh", "refresh_token": "rotated"}
            ),
        ),
        patch(
            "custom_components.usps.account.client.InformedDeliveryClient.async_get_mail_pieces",
            new=AsyncMock(return_value={"mailpieces": []}),
        ),
        patch(
            "custom_components.usps.account.client.InformedDeliveryClient.async_get_packages",
            new=AsyncMock(return_value=[]),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        refresh = AsyncMock()
        with patch.object(
            entry.runtime_data.coordinator, "async_request_refresh", new=refresh
        ):
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, "access_token": "rotated-again"},
            )
            await hass.async_block_till_done()
            assert refresh.call_count == 0

            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_INCLUDE_HISTORY: True}
            )
            await hass.async_block_till_done()
            assert refresh.call_count == 1
