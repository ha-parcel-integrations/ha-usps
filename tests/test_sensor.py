"""Tests for USPS sensor property logic.

The sensors read normalised parcel dicts off ``coordinator.data``; the
``_parcel`` helper here builds that shape directly, bypassing the
raw-to-normalised step each backend's own ``test_parcels.py`` covers.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from custom_components.usps.const import DOMAIN, ParcelStatus
from custom_components.usps.sensor import (
    USPSDeliveredParcelsSensor,
    USPSIncomingParcelsSensor,
    USPSLastUpdateSensor,
    USPSNextDeliverySensor,
    USPSParcelSensor,
)


def _coordinator(data: list[dict] | None, delivered: list[dict] | None = None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.delivered = delivered if delivered is not None else []
    return coordinator


def _entry(entry_id: str = "cfg") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _parcel(
    barcode: str,
    status: ParcelStatus = ParcelStatus.IN_TRANSIT,
    planned_from: str | None = None,
) -> dict:
    return {
        "carrier": "USPS",
        "barcode": barcode,
        "sender": "Sender",
        "receiver": None,
        "status": status,
        "planned_from": planned_from,
        "planned_to": None,
    }


def test_incoming_counts_and_lists_the_active_parcels():
    sensor = USPSIncomingParcelsSensor(
        _coordinator([_parcel("A"), _parcel("B")]), _entry(), MagicMock(), set()
    )
    assert sensor.native_value == 2
    assert len(sensor.extra_state_attributes["parcels"]) == 2


def test_incoming_spawns_a_sensor_for_each_newly_seen_parcel():
    async_add_entities = MagicMock()
    sensor = USPSIncomingParcelsSensor(
        _coordinator([_parcel("A"), _parcel("B")]),
        _entry(),
        async_add_entities,
        known_barcodes={"A"},
    )
    sensor.hass = MagicMock()

    with patch.object(USPSIncomingParcelsSensor.__bases__[0], "_handle_coordinator_update"):
        sensor._handle_coordinator_update()

    spawned = list(async_add_entities.call_args[0][0])
    assert [s.unique_id for s in spawned] == ["cfg_B"]
    assert sensor._known_barcodes == {"A", "B"}


def test_incoming_removes_a_vanished_parcel_through_the_registry():
    """A barcode that falls out of coordinator data must be removed via the
    registry, not by the per-parcel sensor removing itself — self-removal
    races the coordinator-listener cleanup and leaves a ghost entity.
    """
    sensor = USPSIncomingParcelsSensor(
        _coordinator([_parcel("A")]),
        _entry(),
        MagicMock(),
        known_barcodes={"A", "GONE"},
    )
    sensor.hass = MagicMock()

    registry = MagicMock()
    registry.async_get_entity_id.return_value = "sensor.usps_parcel_gone"

    with patch(
        "custom_components.usps.sensor.er.async_get", return_value=registry
    ), patch.object(USPSIncomingParcelsSensor.__bases__[0], "_handle_coordinator_update"):
        sensor._handle_coordinator_update()

    registry.async_get_entity_id.assert_called_once_with("sensor", DOMAIN, "cfg_GONE")
    registry.async_remove.assert_called_once_with("sensor.usps_parcel_gone")


def test_incoming_leaves_the_registry_alone_when_a_parcel_is_only_added():
    sensor = USPSIncomingParcelsSensor(
        _coordinator([_parcel("A")]), _entry(), MagicMock(), known_barcodes=set()
    )
    sensor.hass = MagicMock()

    with patch(
        "custom_components.usps.sensor.er.async_get"
    ) as async_get, patch.object(
        USPSIncomingParcelsSensor.__bases__[0], "_handle_coordinator_update"
    ):
        sensor._handle_coordinator_update()

    async_get.assert_not_called()


def test_parcel_sensor_reports_the_status_and_the_whole_parcel():
    parcel = _parcel("A", status=ParcelStatus.OUT_FOR_DELIVERY)
    sensor = USPSParcelSensor(_coordinator([parcel]), _entry(), "A")
    assert sensor.native_value == ParcelStatus.OUT_FOR_DELIVERY
    assert sensor.extra_state_attributes == parcel


def test_parcel_sensor_is_none_while_its_parcel_is_missing():
    sensor = USPSParcelSensor(_coordinator([_parcel("A")]), _entry(), "MISSING")
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_next_delivery_picks_the_earliest_moment():
    coordinator = _coordinator(
        [
            _parcel("A", planned_from="2026-05-02T10:00:00Z"),
            _parcel("B", planned_from="2026-05-01T10:00:00Z"),
        ]
    )
    sensor = USPSNextDeliverySensor(coordinator, _entry())
    assert sensor.native_value == datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_next_delivery_is_none_without_any_moment():
    sensor = USPSNextDeliverySensor(_coordinator([_parcel("A")]), _entry())
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_next_delivery_skips_an_unparseable_moment():
    coordinator = _coordinator(
        [
            _parcel("A", planned_from="not-a-date"),
            _parcel("B", planned_from="2026-05-01T10:00:00Z"),
        ]
    )
    sensor = USPSNextDeliverySensor(coordinator, _entry())
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_delivered_sensor_counts_the_coordinator_s_delivered_list():
    coordinator = _coordinator(
        [], delivered=[_parcel("D", status=ParcelStatus.DELIVERED)]
    )
    sensor = USPSDeliveredParcelsSensor(coordinator, _entry())
    assert sensor.native_value == 1
    assert sensor.extra_state_attributes["parcels"][0]["barcode"] == "D"


def test_last_update_sensor_reports_the_last_successful_poll():
    coordinator = _coordinator([])
    moment = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    coordinator.last_success_time = moment
    sensor = USPSLastUpdateSensor(coordinator, _entry())
    assert sensor.native_value == moment
