"""Informed Delivery inbox mapping tests."""
from zoneinfo import ZoneInfo

import pytest

from custom_components.usps.account.client import EASTERN
from custom_components.usps.account.parcels import map_informed_delivery_status
from custom_components.usps.account.parcels import (
    normalize_informed_delivery_parcel as _normalize,
)
from custom_components.usps.const import ParcelStatus

PACIFIC = ZoneInfo("America/Los_Angeles")


def normalize_informed_delivery_parcel(raw):
    return _normalize(raw, tz=EASTERN)


def test_inbox_record_maps_to_eastern_delivery_window():
    parcel = normalize_informed_delivery_parcel({"trackingNumber": "9400", "shipperName": "Sender", "eventTimestamp": "2026-01-02T12:00:00Z", "deliveryInfo": {"statusCategory": "Out for Delivery", "status": "Out for Delivery", "deliveryDate": "2026-01-02"}})
    assert parcel["status"] is ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["planned_from"].endswith("-05:00")
    assert parcel["history"] is None and parcel["receiver"] is None


def test_inbox_preparing_for_delivery_is_in_transit():
    parcel = normalize_informed_delivery_parcel({"trackingNumber": "9400", "deliveryInfo": {"statusCategory": "Preparing for Delivery"}})
    assert parcel["status"] is ParcelStatus.IN_TRANSIT


def test_inbox_raw_preserves_the_full_source_record():
    raw = {"trackingNumber": "9400", "nickname": "Private package", "nested": {"packageId": "id"}, "deliveryInfo": {"statusCategory": "Delivered"}}
    assert normalize_informed_delivery_parcel(raw)["raw"] is raw


def test_delivered_at_is_interpreted_as_eastern_local_time():
    parcel = normalize_informed_delivery_parcel(
        {
            "trackingNumber": "9400",
            "eventTimestamp": "2026-08-14T10:29:00",
            "deliveryInfo": {"statusCategory": "Delivered", "status": "Delivered, Front Door/Porch"},
        }
    )
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-08-14T10:29:00-04:00"
    assert parcel["planned_from"] is None and parcel["planned_to"] is None


def test_not_delivered_has_no_delivered_at():
    parcel = normalize_informed_delivery_parcel(
        {"trackingNumber": "9400", "eventTimestamp": "2026-08-14T10:29:00", "deliveryInfo": {"statusCategory": "Out for Delivery"}}
    )
    assert parcel["delivered_at"] is None


def test_missing_or_unparseable_event_timestamp_is_none():
    assert normalize_informed_delivery_parcel({"trackingNumber": "9400", "deliveryInfo": {"statusCategory": "Delivered"}})["delivered_at"] is None
    assert (
        normalize_informed_delivery_parcel(
            {"trackingNumber": "9400", "eventTimestamp": "not-a-timestamp", "deliveryInfo": {"statusCategory": "Delivered"}}
        )["delivered_at"]
        is None
    )


def test_status_falls_back_to_raw_status_when_category_absent():
    parcel = normalize_informed_delivery_parcel({"trackingNumber": "9400", "deliveryInfo": {"status": "Delivered, Front Door/Porch"}})
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Delivered, Front Door/Porch"


def test_missing_delivery_info_defaults_to_unknown():
    parcel = normalize_informed_delivery_parcel({"trackingNumber": "9400"})
    assert parcel["status"] is ParcelStatus.UNKNOWN


def test_planned_to_uses_top_level_expected_delivery_date_fallback():
    parcel = normalize_informed_delivery_parcel(
        {"trackingNumber": "9400", "expectedDeliveryDate": "2026-01-05", "deliveryInfo": {"statusCategory": "Preparing for Delivery"}}
    )
    assert parcel["planned_from"].startswith("2026-01-05")


def test_unparseable_delivery_date_is_none():
    parcel = normalize_informed_delivery_parcel(
        {"trackingNumber": "9400", "deliveryInfo": {"statusCategory": "Preparing for Delivery", "deliveryDate": "not-a-date"}}
    )
    assert parcel["planned_from"] is None


def test_map_narrow_status_warns_once_and_never_reuses_api_tracking_prefixes(caplog):
    # "Pre-Shipment" is a valid API Tracking prefix but not a capture-backed
    # Informed Delivery category — it must stay unknown.
    assert map_informed_delivery_status("Pre-Shipment") is ParcelStatus.UNKNOWN
    assert caplog.text.count("Unrecognised USPS Informed Delivery status") == 1
    caplog.clear()
    assert map_informed_delivery_status("Pre-Shipment") is ParcelStatus.UNKNOWN
    assert "Unrecognised" not in caplog.text


def test_map_narrow_status_none_is_silent():
    assert map_informed_delivery_status(None) is ParcelStatus.UNKNOWN


def test_on_the_way_is_in_transit():
    parcel = normalize_informed_delivery_parcel(
        {"trackingNumber": "9400", "deliveryInfo": {"statusCategory": "On the Way", "status": "Arrived at USPS Facility"}}
    )
    assert parcel["status"] is ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "Arrived at USPS Facility"


def test_accepted_is_in_transit():
    assert map_informed_delivery_status("Accepted") is ParcelStatus.IN_TRANSIT


def test_usps_awaiting_item_is_registered():
    parcel = normalize_informed_delivery_parcel(
        {
            "trackingNumber": "9361",
            "shipperName": "AMAZON",
            "deliveryInfo": {
                "identifier": "UNKNOWN",
                "text1": "Delivery Date Unknown",
                "status": "Picked up by Shipping Partner",
                "statusCategory": "USPS Awaiting Item",
            },
        }
    )
    assert parcel["status"] is ParcelStatus.REGISTERED
    assert parcel["planned_from"] is None and parcel["planned_to"] is None


@pytest.mark.parametrize(
    ("text2", "expected"),
    [
        ("by 9:00pm", "2026-09-28T21:00:00-04:00"),
        ("By 6:30 PM", "2026-09-28T18:30:00-04:00"),
        ("by 12:00pm", "2026-09-28T12:00:00-04:00"),
        ("by 11am", "2026-09-28T11:00:00-04:00"),
    ],
)
def test_planned_to_uses_the_by_time_from_text2(text2, expected):
    parcel = normalize_informed_delivery_parcel(
        {
            "trackingNumber": "9400",
            "deliveryInfo": {"deliveryDate": "2026-09-28", "text2": text2, "statusCategory": "On the Way"},
        }
    )
    assert parcel["planned_from"] == "2026-09-28T00:00:00-04:00"
    assert parcel["planned_to"] == expected


def test_between_times_in_text2_become_the_planned_window():
    parcel = normalize_informed_delivery_parcel(
        {
            "trackingNumber": "9361",
            "deliveryInfo": {
                "deliveryDate": "2026-09-24",
                "identifier": "TODAY",
                "text1": "Expected Delivery",
                "text2": "between 1:00pm and 3:00pm",
                "status": "Out for Delivery, Expected Delivery Between 1:00pm and 3:00pm",
                "statusCategory": "Out for Delivery",
            },
        }
    )
    assert parcel["status"] is ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["planned_from"] == "2026-09-24T13:00:00-04:00"
    assert parcel["planned_to"] == "2026-09-24T15:00:00-04:00"


def test_between_without_minutes_parses():
    parcel = normalize_informed_delivery_parcel(
        {"trackingNumber": "9400", "deliveryInfo": {"deliveryDate": "2026-09-24", "text2": "Between 11am and 1 PM", "statusCategory": "On the Way"}}
    )
    assert parcel["planned_from"] == "2026-09-24T11:00:00-04:00"
    assert parcel["planned_to"] == "2026-09-24T13:00:00-04:00"


@pytest.mark.parametrize("text2", ["between 3:00pm and 1:00pm", "between 13:00pm and 3:00pm", "between 1:00pm and 3:75pm"])
def test_unusable_between_window_falls_back_to_the_whole_day(text2):
    parcel = normalize_informed_delivery_parcel(
        {"trackingNumber": "9400", "deliveryInfo": {"deliveryDate": "2026-09-24", "text2": text2, "statusCategory": "On the Way"}}
    )
    assert parcel["planned_from"] == "2026-09-24T00:00:00-04:00"
    assert parcel["planned_to"] == "2026-09-24T23:59:59.999999-04:00"


@pytest.mark.parametrize("text2", [None, "", "around 2pm", "by 13:00pm", "by 9:75pm", "at 10:29 AM"])
def test_planned_to_falls_back_to_end_of_day(text2):
    parcel = normalize_informed_delivery_parcel(
        {
            "trackingNumber": "9400",
            "deliveryInfo": {"deliveryDate": "2026-09-28", "text2": text2, "statusCategory": "On the Way"},
        }
    )
    assert parcel["planned_to"] == "2026-09-28T23:59:59.999999-04:00"


def test_by_time_without_a_parseable_date_is_none():
    parcel = normalize_informed_delivery_parcel(
        {"trackingNumber": "9400", "deliveryInfo": {"deliveryDate": "not-a-date", "text2": "by 9:00pm", "statusCategory": "On the Way"}}
    )
    assert parcel["planned_to"] is None


def test_package_times_follow_the_given_zone():
    """A Pacific tester's scans only lined up with HA's state changes read as Pacific."""
    parcel = _normalize(
        {
            "trackingNumber": "9361",
            "eventTimestamp": "2026-09-24T07:33:00",
            "deliveryInfo": {"deliveryDate": "2026-09-24", "text2": "between 1:00pm and 3:00pm", "statusCategory": "Out for Delivery"},
        },
        tz=PACIFIC,
    )
    assert parcel["planned_from"] == "2026-09-24T13:00:00-07:00"
    assert parcel["planned_to"] == "2026-09-24T15:00:00-07:00"
    delivered = _normalize(
        {"trackingNumber": "9361", "eventTimestamp": "2026-09-24T14:05:00", "deliveryInfo": {"statusCategory": "Delivered"}},
        tz=PACIFIC,
    )
    assert delivered["delivered_at"] == "2026-09-24T14:05:00-07:00"

