"""Tests for the USPS deliveries calendar."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

from homeassistant.components.calendar import CalendarEvent

from custom_components.usps.calendar import USPSDeliveriesCalendar


def _cal(events: list[CalendarEvent]) -> USPSDeliveriesCalendar:
    entry = MagicMock()
    entry.entry_id = "cfg"
    cal = USPSDeliveriesCalendar(MagicMock(), entry)
    cal._events = lambda: events  # type: ignore[method-assign]
    return cal


ALL_DAY = CalendarEvent(
    start=date(2099, 4, 29), end=date(2099, 4, 30), summary="All day", uid="DAY"
)
TIMED = CalendarEvent(
    start=datetime(2099, 5, 10, 13, 0, tzinfo=UTC),
    end=datetime(2099, 5, 10, 14, 0, tzinfo=UTC),
    summary="Timed",
    uid="TIMED",
)


def test_event_handles_all_day_and_timed_events():
    event = _cal([ALL_DAY, TIMED]).event
    assert event is not None
    assert event.uid in {"DAY", "TIMED"}


def test_event_is_none_without_events():
    assert _cal([]).event is None


async def test_get_events_returns_all_day_and_timed():
    events = await _cal([ALL_DAY, TIMED]).async_get_events(
        MagicMock(),
        datetime(2099, 4, 1, tzinfo=UTC),
        datetime(2099, 5, 30, tzinfo=UTC),
    )
    assert {e.uid for e in events} == {"DAY", "TIMED"}


async def test_get_events_narrow_range_returns_only_the_timed_event():
    events = await _cal([ALL_DAY, TIMED]).async_get_events(
        MagicMock(),
        datetime(2099, 5, 10, 13, 30, tzinfo=UTC),
        datetime(2099, 5, 10, 13, 45, tzinfo=UTC),
    )
    assert [e.uid for e in events] == ["TIMED"]


def _parcel_cal(parcel: dict) -> USPSDeliveriesCalendar:
    entry = MagicMock()
    entry.entry_id = "cfg"
    coordinator = MagicMock()
    coordinator.data = [parcel]
    return USPSDeliveriesCalendar(coordinator, entry)


def test_title_prefers_contents_then_sender_then_fallback():
    base = {"planned_from": "2099-01-01T10:00:00Z", "barcode": "A", "sender": "Shop"}
    assert _parcel_cal({**base, "contents": "Running shoes"}).event.summary == (
        "Running shoes"
    )
    assert _parcel_cal({**base, "contents": None}).event.summary == "Shop"
    no_sender = {**base, "contents": None, "sender": None}
    assert _parcel_cal(no_sender).event.summary == "Parcel A"
    assert _parcel_cal({**no_sender, "barcode": None}).event.summary == "USPS parcel"
