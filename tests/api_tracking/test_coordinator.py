"""Coordinator behaviour specific to explicit-code API Tracking."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.usps.api_tracking.client import USPSApiError
from custom_components.usps.api_tracking.coordinator import (
    BACKOFF_BASE_SECONDS,
    USPSCoordinator,
)
from custom_components.usps.const import CONF_PARCELS, CONF_TRACKING_CODE, DOMAIN


async def test_429_uses_exponential_backoff_when_no_header(hass):
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_PARCELS: [{CONF_TRACKING_CODE: "9400"}]})
    client = MagicMock()
    client.async_get_parcel = AsyncMock(side_effect=USPSApiError("HTTP 429", status_code=429))
    coordinator = USPSCoordinator(hass, client, entry)
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after == BACKOFF_BASE_SECONDS * 2


async def test_delivered_codes_are_skipped_on_subsequent_polls(hass):
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_PARCELS: [{CONF_TRACKING_CODE: "9400"}]})
    client = MagicMock()
    client.async_get_parcel = AsyncMock(return_value={"trackingNumber": "9400", "statusCategory": "Delivered", "trackingEvents": []})
    coordinator = USPSCoordinator(hass, client, entry)
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    assert client.async_get_parcel.call_count == 1


async def test_per_code_failure_keeps_last_good_payload(hass):
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_PARCELS: [{CONF_TRACKING_CODE: "9400"}]})
    client = MagicMock()
    client.async_get_parcel = AsyncMock(side_effect=[{"trackingNumber": "9400", "statusCategory": "Accepted", "trackingEvents": []}, USPSApiError("HTTP 500", status_code=500)])
    coordinator = USPSCoordinator(hass, client, entry)
    first = await coordinator._async_update_data()
    assert await coordinator._async_update_data() == first


async def test_delivery_transition_fires_only_delivered_event(hass):
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_PARCELS: [{CONF_TRACKING_CODE: "9400"}]})
    client = MagicMock()
    client.async_get_parcel = AsyncMock()
    coordinator = USPSCoordinator(hass, client, entry)
    from custom_components.usps.api_tracking.parcels import (
        normalize_api_tracking_parcel,
    )
    from custom_components.usps.const import ParcelStatus

    coordinator._known_state = {"9400": ParcelStatus.IN_TRANSIT}
    coordinator._known_delivery_times = {"9400": (None, None)}
    events = []
    hass.bus.async_listen("usps_parcel_delivered", lambda event: events.append(event))
    coordinator._fire_change_events([normalize_api_tracking_parcel({"trackingNumber": "9400", "statusCategory": "Delivered", "trackingEvents": [{"eventType": "Delivered", "eventTimestamp": "2026-01-02T10:00:00Z"}]})])
    await hass.async_block_till_done()
    assert len(events) == 1
