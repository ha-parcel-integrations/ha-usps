"""The parcel contract's checks, run over every normalised sample."""

from datetime import date, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.usps.account.parcels import normalize_informed_delivery_parcel
from custom_components.usps.api.parcels import (
    normalize_api_tracking_parcel,
    parse_iso,
    planned_window,
)
from custom_components.usps.calendar import USPSDeliveriesCalendar
from custom_components.usps.const import ParcelStatus

ZONE = ZoneInfo("America/New_York")

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def _api(category: str, *events: tuple[str, str]) -> dict:
    return {
        "trackingNumber": "9400111899223197428490",
        "statusCategory": category,
        "trackingEvents": [
            {"eventType": kind, "eventTimestamp": at} for kind, at in events
        ],
    }


def _inbox(category: str, date: str | None = "2099-04-29", text2=None) -> dict:
    return {
        "trackingNumber": "9400111899223197428490",
        "shipperName": "Example Shop",
        "eventTimestamp": "2099-04-29T16:00:00",
        "deliveryInfo": {
            "statusCategory": category,
            "deliveryDate": date,
            "text2": text2,
        },
    }


def _api_parcel(raw: dict) -> dict:
    return normalize_api_tracking_parcel(raw)


def _inbox_parcel(raw: dict) -> dict:
    return normalize_informed_delivery_parcel(raw, tz=ZONE)


SAMPLES = {
    "api_delivered": lambda: _api_parcel(_api("Delivered")),
    "api_active": lambda: _api_parcel(_api("In Transit")),
    "api_held": lambda: _api_parcel(
        _api("Available for Pickup", ("In Transit", "2099-04-28T09:00:00Z"))
    ),
    "api_collected": lambda: _api_parcel(
        _api(
            "Delivered",
            ("Available for Pickup", "2099-04-28T09:00:00Z"),
            ("Delivered", "2099-04-29T16:00:00Z"),
        )
    ),
    "api_returning": lambda: _api_parcel(_api("Return to Sender")),
    "api_bare": lambda: _api_parcel({"trackingNumber": "9400111899223197428490"}),
    "inbox_delivered": lambda: _inbox_parcel(_inbox("Delivered")),
    "inbox_out": lambda: _inbox_parcel(_inbox("Out for Delivery")),
    "inbox_by": lambda: _inbox_parcel(_inbox("Out for Delivery", text2="by 9:00pm")),
    "inbox_between": lambda: _inbox_parcel(
        _inbox("Out for Delivery", text2="between 1:00pm and 3:00pm")
    ),
    "inbox_bare": lambda: _inbox_parcel({"trackingNumber": "9400111899223197428490"}),
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return SAMPLES[request.param]()


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_collected_api_parcel_stays_pickup():
    parcel = SAMPLES["api_collected"]()
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["delivered_at"] == "2099-04-29T16:00:00Z"


def test_a_held_api_parcel_is_pickup_and_an_ordinary_one_is_not():
    assert SAMPLES["api_held"]()["pickup"] is True
    assert SAMPLES["api_active"]()["pickup"] is False
    assert SAMPLES["api_delivered"]()["pickup"] is False


def test_a_returning_parcel_keeps_its_route_and_is_not_delivered():
    parcel = _api_parcel(
        _api("Return to Sender", ("Available for Pickup", "2099-04-28T09:00:00Z"))
    )
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["pickup"] is True
    assert parcel["delivered"] is False


def test_informed_delivery_has_no_pickup_points():
    assert SAMPLES["inbox_out"]()["pickup"] is False


def test_a_date_covers_the_whole_day():
    parcel = SAMPLES["inbox_out"]()
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert start.date() == end.date()
    assert start.utcoffset() == end.utcoffset() == ZONE.utcoffset(start)


def test_a_by_time_keeps_its_end_and_a_between_keeps_both_ends():
    by = SAMPLES["inbox_by"]()
    assert datetime.fromisoformat(by["planned_to"]).hour == 21
    between = SAMPLES["inbox_between"]()
    assert datetime.fromisoformat(between["planned_from"]).hour == 13
    assert datetime.fromisoformat(between["planned_to"]).hour == 15


def test_an_api_estimate_moment_would_end_at_the_end_of_that_day():
    start, end = planned_window("2099-04-29T13:00:00Z", None, zone=ZONE)
    stop = datetime.fromisoformat(end)
    assert (stop.hour, stop.minute, stop.second, stop.microsecond) == (23, 59, 59, 0)
    assert stop.date() == parse_iso(start).astimezone(ZONE).date()


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2099-04-29", "2099-05-01"),
        ("2099-04-29T13:00:00", None),
        ("2099-04-29T15:00:00Z", "2099-04-29T13:00:00Z"),
        ("not-a-date", None),
        (None, "2099-04-29"),
        (None, None),
        (12, None),
    ],
)
def test_planned_window_shapes_are_ordered(start, end):
    first, last = planned_window(start, end, zone=ZONE)
    if last is not None:
        assert parse_iso(last) > parse_iso(first)
    if first is None:
        assert last is None


def test_calendar_shows_a_whole_day_window_as_an_all_day_event():
    coordinator = MagicMock()
    coordinator.data = [
        {
            "barcode": "X",
            "planned_from": "2099-04-29T00:00:00-04:00",
            "planned_to": "2099-04-29T23:59:59-04:00",
        },
        {
            "barcode": "Y",
            "planned_from": "2099-04-29T13:00:00-04:00",
            "planned_to": "2099-04-29T23:59:59-04:00",
        },
    ]
    entry = MagicMock()
    entry.entry_id = "cfg"
    events = {e.uid: e for e in USPSDeliveriesCalendar(coordinator, entry)._events()}
    assert events["X"].start == date(2099, 4, 29)
    assert events["X"].end == date(2099, 4, 30)
    assert isinstance(events["Y"].start, datetime)
