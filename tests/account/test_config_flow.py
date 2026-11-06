"""Informed Delivery config-flow tests (browser-session handoff)."""

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.usps.account.auth import enrolled_addresses
from custom_components.usps.api.client import USPSApiError, USPSAuthError
from custom_components.usps.const import (
    CONF_REFRESH_TOKEN,
    CONF_SESSION_TOKEN,
    CONF_SOURCE,
    CONF_USERNAME,
    DOMAIN,
    SOURCE_INFORMED_DELIVERY,
)


def test_unenrolled_profile_is_empty():
    assert enrolled_addresses({"addresses": [{"granted_services": ["MYPOST"]}]}) == []


def _rmin_profile():
    return {
        "addresses": [
            {
                "ZIPCode": "12345",
                "ZIPPlus4": "6789",
                "deliveryPoint": "01",
                "granted_services": ["RMIN"],
            }
        ]
    }


def _informed_unique_id(username: str) -> str:
    account = hashlib.sha256(username.lower().encode()).hexdigest()[:12]
    return f"{SOURCE_INFORMED_DELIVERY}:{account}"


def _fake_journey(*, tokens=None, profile=None, finish_error=None):
    """A LoginJourney stand-in exercising only the handoff's finish() leg."""
    journey = MagicMock()
    if finish_error is not None:
        journey.finish = AsyncMock(side_effect=finish_error)
    else:
        journey.finish = AsyncMock(
            return_value=(
                tokens or {"access_token": "access", "refresh_token": "refresh"},
                profile if profile is not None else _rmin_profile(),
            )
        )
    return journey


async def _submit_handoff(hass, journey, *, username="user@example.test"):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY}
    )
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: username, CONF_SESSION_TOKEN: "  cookie-value  "},
        )


async def test_handoff_creates_an_entry_from_an_rmin_session(hass):
    journey = _fake_journey()
    result = await _submit_handoff(hass, journey)
    assert result["type"] == "create_entry"
    assert result["data"][CONF_SOURCE] == SOURCE_INFORMED_DELIVERY
    assert result["data"][CONF_REFRESH_TOKEN] == "refresh"
    # The pasted cookie is whitespace-trimmed and handed straight to finish();
    # it is traded for the refresh token and never stored on the entry.
    journey.finish.assert_awaited_once_with("cookie-value")
    assert CONF_SESSION_TOKEN not in result["data"]


async def test_handoff_aborts_without_an_enrolled_address(hass):
    journey = _fake_journey(profile={"addresses": []})
    result = await _submit_handoff(hass, journey)
    assert result["type"] == "form"
    assert result["errors"]["base"] == "not_enrolled"


async def test_handoff_rejects_an_invalid_session(hass):
    """A missing/expired cookie never reaches an authorization code, so finish()
    raises USPSAuthError — surfaced as a session problem, not wrong credentials."""
    journey = _fake_journey(finish_error=USPSAuthError("Authorization code missing"))
    result = await _submit_handoff(hass, journey)
    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_session"


async def test_handoff_surfaces_cannot_connect(hass):
    journey = _fake_journey(finish_error=USPSApiError("HTTP 503", status_code=503))
    result = await _submit_handoff(hass, journey)
    assert result["errors"]["base"] == "cannot_connect"


async def test_reauth_keeps_the_informed_delivery_source(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=_informed_unique_id("user@example.test"),
        data={
            CONF_SOURCE: SOURCE_INFORMED_DELIVERY,
            CONF_USERNAME: "user@example.test",
            "access_token": "old",
            CONF_REFRESH_TOKEN: "old-refresh",
            "addresses": [{"zip11": "12345678901"}],
        },
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == SOURCE_INFORMED_DELIVERY
    journey = _fake_journey()
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.test", CONF_SESSION_TOKEN: "cookie"},
        )
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "refresh"
    await hass.async_block_till_done()


async def test_reauth_prefills_the_entry_username(hass):
    """The entry's unique_id is derived from the username, so reauth must offer
    it back rather than make the user retype it and risk a mismatch."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=_informed_unique_id("user@example.test"),
        data={
            CONF_SOURCE: SOURCE_INFORMED_DELIVERY,
            CONF_USERNAME: "user@example.test",
            CONF_REFRESH_TOKEN: "old-refresh",
        },
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    schema = result["data_schema"].schema
    username_key = next(k for k in schema if k == CONF_USERNAME)
    assert username_key.description == {"suggested_value": "user@example.test"}


async def test_reauth_with_another_account_aborts(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=_informed_unique_id("user@example.test"),
        data={
            CONF_SOURCE: SOURCE_INFORMED_DELIVERY,
            CONF_USERNAME: "user@example.test",
            CONF_REFRESH_TOKEN: "old-refresh",
        },
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    journey = _fake_journey()
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "other@example.test", CONF_SESSION_TOKEN: "cookie"},
        )
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_REFRESH_TOKEN] == "old-refresh"
