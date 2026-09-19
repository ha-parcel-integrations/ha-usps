"""Tests for the dynamic, status-driven polling algorithm (CLAUDE.md).

Pure-function tests for the tiering/scheduling helpers, plus a few
integration checks that ``_async_update_data`` actually wires them up (the
full-stop condition, its resume, and the 429 backoff).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.usps.api.client import USPSApiError
from custom_components.usps.api.coordinator import (
    USPSCoordinator,
    _hottest_tier_minutes,
    _in_quiet_window,
    _next_anchor,
    _next_update_interval,
    _stagger_minutes,
)
from custom_components.usps.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    MID_INTERVAL_MINUTES,
    STAGGER_MINUTES,
)

UTC = timezone.utc
ACTIVE_CODE = "9400111899223197428490"


def _out_for_delivery(planned_from: str | None) -> dict:
    return {"status": "out_for_delivery", "planned_from": planned_from}


def _mid(status: str = "in_transit") -> dict:
    return {"status": status, "planned_from": None}


def _active_payload(code: str = ACTIVE_CODE) -> dict:
    return {
        "trackingNumber": code,
        "statusCategory": "Out for Delivery",
        "status": "Out for Delivery",
        "trackingEvents": [],
    }


# ---------------------------------------------------------------------------
# _in_quiet_window / _next_anchor
# ---------------------------------------------------------------------------


def test_quiet_window_is_midnight_to_six():
    assert _in_quiet_window(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    assert _in_quiet_window(datetime(2026, 1, 1, 5, 59, tzinfo=UTC))
    assert not _in_quiet_window(datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    assert not _in_quiet_window(datetime(2026, 1, 1, 23, 59, tzinfo=UTC))


def test_next_anchor_before_six_is_six_today():
    now = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)
    assert _next_anchor(now) == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def test_next_anchor_after_six_is_midnight_tomorrow():
    now = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    assert _next_anchor(now) == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# _stagger_minutes
# ---------------------------------------------------------------------------


def test_stagger_is_stable_and_bounded():
    a = _stagger_minutes("entry-1")
    b = _stagger_minutes("entry-1")
    c = _stagger_minutes("entry-2")
    assert a == b
    assert 0 <= a < STAGGER_MINUTES
    assert 0 <= c < STAGGER_MINUTES


# ---------------------------------------------------------------------------
# _hottest_tier_minutes
# ---------------------------------------------------------------------------


def test_tier_is_none_when_nothing_active():
    assert _hottest_tier_minutes([], datetime(2026, 1, 1, 12, tzinfo=UTC)) is None


def test_tier_is_mid_for_non_hot_statuses():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [_mid("registered"), _mid("problem"), _mid("returning")]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


def test_tier_is_hot_when_out_for_delivery_without_planned_from():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [_mid(), _out_for_delivery(None)]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_hot_when_planned_from_is_unparseable():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [_out_for_delivery("not-a-date")]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_hot_within_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(minutes=30)  # inside the 1h lookahead
    parcels = [_out_for_delivery(planned.isoformat())]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_mid_before_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(hours=3)  # well outside the 1h lookahead
    parcels = [_out_for_delivery(planned.isoformat())]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


# ---------------------------------------------------------------------------
# _next_update_interval
# ---------------------------------------------------------------------------


def test_none_tier_fully_suspends():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert _next_update_interval(now, None, "entry-1") is None


def test_daytime_candidate_outside_window_is_tier_plus_stagger():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    interval = _next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1")
    stagger = _stagger_minutes("entry-1")
    assert interval == timedelta(minutes=MID_INTERVAL_MINUTES + stagger)


def test_now_inside_quiet_window_jumps_to_next_anchor():
    now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)  # an anchor poll itself
    interval = _next_update_interval(now, HOT_INTERVAL_MINUTES, "entry-1")
    assert now + interval == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def test_candidate_landing_in_quiet_window_clamps_to_the_midnight_anchor():
    # 23:50 + 45 min mid tier would land at 00:35, inside the quiet window —
    # clamps forward to the *nearest* anchor (00:00), not all the way to 06:00.
    now = datetime(2026, 1, 1, 23, 50, tzinfo=UTC)
    interval = _next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1")
    assert now + interval == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# integration: full stop, resume, and the 429 backoff
# ---------------------------------------------------------------------------


def _entry_with(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_PARCELS: parcels,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
        unique_id=DOMAIN,
    )


async def test_update_interval_none_when_nothing_tracked(hass):
    entry = _entry_with([])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = USPSCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator.update_interval is None
    assert coordinator.current_tier_minutes is None


async def test_update_interval_resumes_once_a_parcel_is_added(hass):
    entry = _entry_with([])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = USPSCoordinator(hass, client, entry)
    await coordinator._async_update_data()
    assert coordinator.update_interval is None

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]}
    )
    client.async_get_parcel.return_value = _active_payload()
    await coordinator._async_update_data()

    assert coordinator.update_interval is not None
    assert coordinator.current_tier_minutes == HOT_INTERVAL_MINUTES


async def test_429_raises_update_failed_with_retry_after(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = USPSApiError(
        "HTTP 429", status_code=429, retry_after=120
    )
    coordinator = USPSCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed) as excinfo:
        await coordinator._async_update_data()

    assert excinfo.value.retry_after == 120


async def test_429_backoff_grows_without_a_retry_after_header(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = USPSApiError(
        "HTTP 429", status_code=429, retry_after=None
    )
    coordinator = USPSCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed) as first:
        await coordinator._async_update_data()
    with pytest.raises(UpdateFailed) as second:
        await coordinator._async_update_data()

    assert first.value.retry_after is not None
    assert second.value.retry_after > first.value.retry_after


async def test_unknown_code_falls_back_to_a_pending_placeholder(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = None
    coordinator = USPSCoordinator(hass, client, entry)
    active = await coordinator._async_update_data()
    assert active[0]["barcode"] == ACTIVE_CODE


async def test_all_fetches_failing_raises_update_failed(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = USPSApiError("HTTP 500", status_code=500)
    coordinator = USPSCoordinator(hass, client, entry)
    with pytest.raises(UpdateFailed, match="unreachable"):
        await coordinator._async_update_data()


async def test_registered_event_fires_for_a_new_not_yet_delivered_parcel(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = _active_payload()
    coordinator = USPSCoordinator(hass, client, entry)
    coordinator._known_state = {}
    events = []
    hass.bus.async_listen("usps_parcel_registered", lambda event: events.append(event))
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_delivery_time_change_fires_its_own_event(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = USPSCoordinator(hass, client, entry)
    coordinator._known_state = {ACTIVE_CODE: "out_for_delivery"}
    coordinator._known_delivery_times = {ACTIVE_CODE: (None, None)}
    events = []
    hass.bus.async_listen(
        "usps_parcel_delivery_time_changed", lambda event: events.append(event)
    )
    coordinator._fire_change_events(
        [
            {
                "barcode": ACTIVE_CODE,
                "status": "out_for_delivery",
                "planned_from": "2026-01-01T12:00:00+00:00",
                "planned_to": "2026-01-01T14:00:00+00:00",
            }
        ]
    )
    await hass.async_block_till_done()
    assert len(events) == 1
