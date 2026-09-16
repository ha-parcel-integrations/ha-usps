"""API Tracking canonical mapping tests."""
from unittest.mock import MagicMock

from custom_components.usps.api_tracking.parcels import (
    apply_delivered_filter,
    build_history,
    format_dimensions,
    map_event_status,
    map_parcel_status,
    normalize_api_tracking_parcel,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
    tracking_url,
)
from custom_components.usps.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    ParcelStatus,
)


def test_detail_mapping_is_conservative_and_preserves_history():
    raw = {"trackingNumber": "9400", "statusCategory": "Delivered", "status": "Delivered, Front Door", "trackingEvents": [{"eventType": "USPS in possession of item", "eventCode": "03", "eventTimestamp": "2026-01-01T10:00:00Z"}, {"eventType": "Delivered", "eventCode": "01", "eventTimestamp": "2026-01-02T10:00:00Z"}]}
    parcel = normalize_api_tracking_parcel(raw, include_history=True)
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered_at"] == "2026-01-02T10:00:00Z"
    assert parcel["sender"] is None and parcel["history"][0]["raw_status"] == "03"


def test_missing_tracking_number_falls_back_to_the_configured_code():
    parcel = normalize_api_tracking_parcel({"statusCategory": "Accepted"}, configured_code="9400")
    assert parcel["barcode"] == "9400"


def test_history_omitted_when_option_is_off():
    parcel = normalize_api_tracking_parcel({"trackingNumber": "9400", "statusCategory": "Accepted", "trackingEvents": [{"eventType": "Accepted", "eventTimestamp": "2026-01-01T10:00:00Z"}]})
    assert parcel["history"] is None


def test_unmapped_status_warns_once(caplog):
    assert map_parcel_status("Something USPS invented") is ParcelStatus.UNKNOWN
    assert caplog.text.count("Unrecognised USPS status") == 1
    caplog.clear()
    assert map_parcel_status("Something USPS invented") is ParcelStatus.UNKNOWN
    assert "Unrecognised USPS status" not in caplog.text


def test_map_parcel_status_none_code_is_silently_unknown(caplog):
    assert map_parcel_status(None) is ParcelStatus.UNKNOWN
    assert caplog.text == ""


def test_map_event_status_none_for_missing_or_unmapped_code():
    assert map_event_status(None) is None
    assert map_event_status("a status nobody has ever seen") is None


def test_map_event_status_matches_a_known_prefix():
    assert map_event_status("Delivered, Front Door") is ParcelStatus.DELIVERED


def test_parse_iso_rejects_garbage_and_defaults_naive_to_utc():
    assert parse_iso(None) is None
    assert parse_iso("not-a-date") is None
    parsed = parse_iso("2026-01-01T10:00:00")
    assert parsed.tzinfo is not None


def test_to_iso_timestamp_handles_epoch_ms_and_overflow():
    assert to_iso_timestamp(None) is None
    assert to_iso_timestamp("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00Z"
    assert to_iso_timestamp(1767225600000).startswith("2025") or to_iso_timestamp(1767225600000).startswith("2026")
    assert to_iso_timestamp(10**20) is None


def test_format_dimensions_requires_all_three():
    assert format_dimensions(None, 1, 1) is None
    assert format_dimensions(30, 20, 10)["text"] == "30 x 20 x 10 cm"


def test_build_history_skips_non_dict_and_undated_events_and_caps_length():
    events = ["not-a-dict", {"eventType": "Accepted"}] + [
        {"eventType": "In Transit", "eventTimestamp": f"2026-01-{day:02d}T00:00:00Z"} for day in range(1, 25)
    ]
    history = build_history(events, max_events=5)
    assert len(history) == 5
    assert history[-1]["timestamp"].startswith("2026-01-24")


def test_build_history_keeps_unparseable_timestamps_at_the_end():
    events = [{"eventType": "In Transit", "eventTimestamp": "garbage-timestamp"}]
    history = build_history(events)
    assert history[0]["timestamp"] == "garbage-timestamp"


def test_sort_parcels_by_ts_puts_unparseable_last_regardless_of_direction():
    parcels = [{"planned_from": None}, {"planned_from": "2026-01-01T00:00:00Z"}]
    assert sort_parcels_by_ts(parcels, "planned_from")[-1]["planned_from"] is None
    assert sort_parcels_by_ts(parcels, "planned_from", descending=True)[-1]["planned_from"] is None


def test_tracking_url_none_for_empty_code():
    assert tracking_url(None) is None
    assert tracking_url("9400").startswith("https://")


def test_apply_delivered_filter_by_parcel_count():
    entry = MagicMock(options={CONF_DELIVERED_FILTER_TYPE: "parcels", CONF_DELIVERED_FILTER_AMOUNT: 1})
    parcels = [{"delivered_at": "2026-01-02T00:00:00Z"}, {"delivered_at": "2026-01-01T00:00:00Z"}]
    assert apply_delivered_filter(parcels, entry) == parcels[:1]


def test_apply_delivered_filter_by_days_keeps_unparseable():
    entry = MagicMock(options={CONF_DELIVERED_FILTER_TYPE: "days", CONF_DELIVERED_FILTER_AMOUNT: 7})
    parcels = [{"delivered_at": None}]
    assert apply_delivered_filter(parcels, entry) == parcels
