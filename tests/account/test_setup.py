"""Informed Delivery setup smoke test for shared HA platforms."""

from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.usps.api.client import USPSAuthError
from custom_components.usps.const import (
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
