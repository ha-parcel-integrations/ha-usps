"""API Tracking source-specific config flow tests."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.usps.api.client import (
    TrackingNotEnabledError,
    USPSApiError,
    USPSAuthError,
)
from custom_components.usps.const import (
    CONF_CONSUMER_KEY,
    CONF_CONSUMER_SECRET,
    CONF_SOURCE,
    DOMAIN,
    SOURCE_API_TRACKING,
)


async def test_api_tracking_reauth_updates_only_api_credentials(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE: SOURCE_API_TRACKING,
            CONF_CONSUMER_KEY: "old",
            CONF_CONSUMER_SECRET: "old-secret",
        },
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    with patch(
        "custom_components.usps.config_flow.ApiTrackingClient.async_validate_credentials",
        new=AsyncMock(return_value=None),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CONSUMER_KEY: "new", CONF_CONSUMER_SECRET: "new-secret"},
        )
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_CONSUMER_KEY] == "new"
    assert entry.data[CONF_SOURCE] == SOURCE_API_TRACKING
    # A successful reauth updates the entry, which schedules both a reload and
    # a delayed write of the config-entry store. Let them finish inside the
    # test rather than leaving a timer behind for whatever runs next.
    await hass.async_block_till_done()


async def test_reauth_confirm_surfaces_invalid_auth(hass):

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE: SOURCE_API_TRACKING,
            CONF_CONSUMER_KEY: "old",
            CONF_CONSUMER_SECRET: "old-secret",
        },
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with patch(
        "custom_components.usps.config_flow.ApiTrackingClient.async_validate_credentials",
        new=AsyncMock(side_effect=USPSAuthError("no")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CONSUMER_KEY: "new", CONF_CONSUMER_SECRET: "new-secret"},
        )
    assert result["errors"]["base"] == "invalid_auth"


async def test_reauth_confirm_surfaces_cannot_connect(hass):

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE: SOURCE_API_TRACKING,
            CONF_CONSUMER_KEY: "old",
            CONF_CONSUMER_SECRET: "old-secret",
        },
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with patch(
        "custom_components.usps.config_flow.ApiTrackingClient.async_validate_credentials",
        new=AsyncMock(side_effect=USPSApiError("no")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CONSUMER_KEY: "new", CONF_CONSUMER_SECRET: "new-secret"},
        )
    assert result["errors"]["base"] == "cannot_connect"


async def test_api_tracking_setup_creates_entry(hass):

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": SOURCE_API_TRACKING}
    )
    with patch(
        "custom_components.usps.config_flow.ApiTrackingClient.async_validate_credentials",
        new=AsyncMock(return_value=None),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CONSUMER_KEY: "key", CONF_CONSUMER_SECRET: "secret"},
        )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_SOURCE] == SOURCE_API_TRACKING


async def test_api_tracking_setup_surfaces_tracking_not_enabled(hass):

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": SOURCE_API_TRACKING}
    )
    with patch(
        "custom_components.usps.config_flow.ApiTrackingClient.async_validate_credentials",
        new=AsyncMock(side_effect=TrackingNotEnabledError("no")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CONSUMER_KEY: "key", CONF_CONSUMER_SECRET: "secret"},
        )
    assert result["errors"]["base"] == "tracking_not_enabled"


async def test_api_tracking_setup_surfaces_invalid_auth(hass):

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": SOURCE_API_TRACKING}
    )
    with patch(
        "custom_components.usps.config_flow.ApiTrackingClient.async_validate_credentials",
        new=AsyncMock(side_effect=USPSAuthError("no")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CONSUMER_KEY: "key", CONF_CONSUMER_SECRET: "secret"},
        )
    assert result["errors"]["base"] == "invalid_auth"


async def test_api_tracking_setup_surfaces_cannot_connect(hass):

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": SOURCE_API_TRACKING}
    )
    with patch(
        "custom_components.usps.config_flow.ApiTrackingClient.async_validate_credentials",
        new=AsyncMock(side_effect=USPSApiError("no")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CONSUMER_KEY: "key", CONF_CONSUMER_SECRET: "secret"},
        )
    assert result["errors"]["base"] == "cannot_connect"


async def test_second_entry_for_the_same_key_aborts_already_configured(hass):

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE: SOURCE_API_TRACKING,
            CONF_CONSUMER_KEY: "key",
            CONF_CONSUMER_SECRET: "secret",
        },
        unique_id="api_tracking:2c70e12b7a06",
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": SOURCE_API_TRACKING}
    )
    with patch(
        "custom_components.usps.config_flow.ApiTrackingClient.async_validate_credentials",
        new=AsyncMock(return_value=None),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CONSUMER_KEY: "key", CONF_CONSUMER_SECRET: "secret"},
        )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
