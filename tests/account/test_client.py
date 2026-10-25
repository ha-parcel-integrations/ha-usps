"""Tests for isolated Informed Delivery token handling."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest

from custom_components.usps.account.client import (
    InformedDeliveryClient,
    InformedDeliveryTokenExpired,
)
from custom_components.usps.account.coordinator import InformedDeliveryCoordinator
from custom_components.usps.api.client import USPSApiError, USPSAuthError


async def test_refresh_replaces_both_rotating_tokens():
    response = AsyncMock(status=200)
    response.json = AsyncMock(
        return_value={"access_token": "new-access", "refresh_token": "new-refresh"}
    )
    context = MagicMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    )
    session = MagicMock()
    session.post.return_value = context
    client = InformedDeliveryClient("old-access", "old-refresh", session)
    await client.async_refresh()
    assert client.refresh_token == "new-refresh"


async def test_100_item_inbox_cap_warns(caplog):
    response = AsyncMock(status=200)
    response.json = AsyncMock(
        return_value={
            "inboundPackages": [
                {"trackingNumber": str(number)} for number in range(100)
            ]
        }
    )
    session = MagicMock()
    session.post.return_value = MagicMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    )
    packages = await InformedDeliveryClient(
        "access", "refresh", session
    ).async_get_packages("12345678901")
    assert len(packages) == 100
    assert "may be truncated" in caplog.text


async def test_401_with_unrecognised_code_is_a_revoked_session():
    response = AsyncMock(status=401)
    response.json = AsyncMock(return_value={"code": "SOMETHING_ELSE"})
    session = MagicMock()
    session.post.return_value = MagicMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    )
    with pytest.raises(USPSAuthError, match="revoked"):
        await InformedDeliveryClient("access", "refresh", session).async_get_packages(
            "12345678901"
        )


async def test_401_with_unparseable_body_is_treated_as_expired():

    response = AsyncMock(status=401)
    response.json = AsyncMock(side_effect=ValueError("bad json"))
    session = MagicMock()
    session.post.return_value = MagicMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    )
    with pytest.raises(InformedDeliveryTokenExpired):
        await InformedDeliveryClient("access", "refresh", session).async_get_packages(
            "12345678901"
        )


async def test_non_200_non_special_status_is_an_api_error():
    response = AsyncMock(status=500)
    session = MagicMock()
    session.post.return_value = MagicMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    )
    with pytest.raises(USPSApiError):
        await InformedDeliveryClient("access", "refresh", session).async_get_packages(
            "12345678901"
        )


async def test_unexpected_non_dict_body_is_an_api_error():
    response = AsyncMock(status=200)
    response.json = AsyncMock(return_value=["not", "a", "dict"])
    session = MagicMock()
    session.post.return_value = MagicMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    )
    with pytest.raises(USPSApiError, match="unexpected"):
        await InformedDeliveryClient("access", "refresh", session).async_get_packages(
            "12345678901"
        )


async def test_empty_204_inbox_is_not_an_error():
    response = AsyncMock(status=204)
    session = MagicMock()
    session.post.return_value = MagicMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    )
    assert (
        await InformedDeliveryClient("access", "refresh", session).async_get_packages(
            "12345678901"
        )
        == []
    )


async def test_rate_limit_preserves_retry_after():
    response = AsyncMock(status=429, headers={"Retry-After": "30"})
    session = MagicMock()
    session.post.return_value = MagicMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    )
    with pytest.raises(USPSApiError) as err:
        await InformedDeliveryClient("access", "refresh", session).async_get_packages(
            "12345678901"
        )
    assert err.value.retry_after == 30


async def test_unparseable_retry_after_is_safe():
    response = AsyncMock(status=429, headers={"Retry-After": "later"})
    session = MagicMock()
    session.post.return_value = MagicMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    )
    with pytest.raises(USPSApiError) as err:
        await InformedDeliveryClient("access", "refresh", session).async_get_packages(
            "12345678901"
        )
    assert err.value.retry_after is None


async def test_html_edge_page_is_an_api_error():
    response = AsyncMock(status=200)
    response.json = AsyncMock(side_effect=ValueError("unexpected mimetype"))
    session = MagicMock()
    session.post.return_value = MagicMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    )
    with pytest.raises(USPSApiError, match="unparseable"):
        await InformedDeliveryClient("access", "refresh", session).async_get_packages(
            "12345678901"
        )


async def test_inbox_coordinator_keeps_detected_duplicate_and_marks_provenance(hass):
    entry = MagicMock(entry_id="entry", data={"addresses": [{"zip11": "12345678901"}]})
    client = AsyncMock()
    detected = {
        "trackingNumber": "9400",
        "_user_added": False,
        "deliveryInfo": {"statusCategory": "Out for Delivery"},
    }
    added = {
        "trackingNumber": "9400",
        "_user_added": True,
        "deliveryInfo": {"statusCategory": "Delivered"},
    }
    client.async_get_packages.return_value = [detected, added]
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    parcels = await coordinator._async_update_data()
    assert len(parcels) == 1
    assert parcels[0]["raw"] is detected
    assert detected["_duplicate_sources"] == ["addedPackages"]


async def test_temporary_inbox_failure_keeps_last_good_data(hass):
    entry = MagicMock(entry_id="entry", data={"addresses": [{"zip11": "12345678901"}]})
    client = AsyncMock()
    client.async_get_packages.side_effect = [
        [
            {
                "trackingNumber": "9400",
                "deliveryInfo": {"statusCategory": "Out for Delivery"},
            }
        ],
        USPSApiError("HTTP 500"),
    ]
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    first = await coordinator._async_update_data()
    assert await coordinator._async_update_data() == first


async def test_keepalive_starts_reauth_after_dead_refresh(hass):
    entry = MagicMock(entry_id="entry", data={"source": "informed_delivery"})
    client = MagicMock()
    client.async_refresh = AsyncMock(side_effect=USPSAuthError("dead"))
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    captured = {}
    with patch(
        "custom_components.usps.account.coordinator.async_track_time_interval",
        side_effect=lambda _hass, callback, _interval: (
            captured.setdefault("callback", callback) or MagicMock()
        ),
    ):
        coordinator.async_start_keepalive()
    await captured["callback"](None)
    entry.async_start_reauth.assert_called_once_with(hass)


def test_keepalive_interval_stays_inside_refresh_lifetime(hass):
    client = MagicMock()
    entry = MagicMock(entry_id="entry", data={})
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    assert coordinator.update_interval.total_seconds() < 15 * 60


def test_current_tier_and_delivered_codes_properties(hass):
    client = MagicMock()
    entry = MagicMock(entry_id="entry", data={})
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    assert coordinator.current_tier_minutes == 10
    assert coordinator.delivered_codes == set()


async def test_keepalive_success_persists_the_rotated_refresh_token(hass):
    entry = MagicMock(entry_id="entry", data={"source": "informed_delivery"})
    client = MagicMock()
    client.async_refresh = AsyncMock(return_value=None)
    client.refresh_token = "new-refresh"
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    captured = {}
    with patch(
        "custom_components.usps.account.coordinator.async_track_time_interval",
        side_effect=lambda _hass, callback, _interval: (
            captured.setdefault("callback", callback) or MagicMock()
        ),
    ):
        coordinator.async_start_keepalive()
    with patch.object(hass.config_entries, "async_update_entry") as update_entry:
        await captured["callback"](None)
    update_entry.assert_called_once()
    assert update_entry.call_args.kwargs["data"]["refresh_token"] == "new-refresh"


async def test_token_expired_refreshes_once_and_retries(hass):
    entry = MagicMock(entry_id="entry", data={"addresses": [{"zip11": "12345678901"}]})
    client = AsyncMock()
    client.refresh_generation = 1
    client.refresh_token = "new-refresh"
    client.async_get_packages.side_effect = [
        InformedDeliveryTokenExpired("expired"),
        [{"trackingNumber": "9400", "deliveryInfo": {"statusCategory": "Delivered"}}],
    ]
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    with patch.object(hass.config_entries, "async_update_entry") as update_entry:
        parcels = await coordinator._async_update_data()
    assert client.async_refresh.await_count == 1
    assert update_entry.called
    assert coordinator.delivered and coordinator.delivered[0]["barcode"] == "9400"
    assert parcels == []


async def test_token_expired_dead_refresh_chain_raises_auth_failed(hass):

    entry = MagicMock(entry_id="entry", data={"addresses": [{"zip11": "12345678901"}]})
    client = AsyncMock()
    client.refresh_generation = 1
    client.async_get_packages.side_effect = InformedDeliveryTokenExpired("expired")
    client.async_refresh.side_effect = USPSAuthError("dead chain", status_code=400)
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_auth_error_directly_raises_auth_failed(hass):

    entry = MagicMock(entry_id="entry", data={"addresses": [{"zip11": "12345678901"}]})
    client = AsyncMock()
    client.async_get_packages.side_effect = USPSAuthError("revoked", status_code=401)
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_api_error_with_no_last_good_data_raises_update_failed(hass):

    entry = MagicMock(entry_id="entry", data={"addresses": [{"zip11": "12345678901"}]})
    client = AsyncMock()
    client.async_get_packages.side_effect = USPSApiError("HTTP 500")
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


def test_keepalive_registers_a_ten_minute_unload_callback(hass):
    client = MagicMock()
    entry = MagicMock(entry_id="entry", data={})
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    cancel = MagicMock()
    with patch(
        "custom_components.usps.account.coordinator.async_track_time_interval",
        return_value=cancel,
    ) as tracker:
        assert coordinator.async_start_keepalive() is cancel
    assert tracker.call_args.args[2].total_seconds() == 600


def _row(code, category, **extra):
    return {
        "trackingNumber": code,
        "deliveryInfo": {"statusCategory": category},
        **extra,
    }


async def _inbox_coordinator(hass, *polls):
    entry = MagicMock(entry_id="entry", data={"addresses": [{"zip11": "12345678901"}]})
    client = AsyncMock()
    client.async_get_packages.side_effect = list(polls)
    events = []
    for name in (
        "registered",
        "status_changed",
        "delivered",
        "delivery_time_changed",
    ):
        hass.bus.async_listen(
            f"usps_parcel_{name}", lambda event, name=name: events.append(name)
        )
    return InformedDeliveryCoordinator(hass, client, entry), events


async def test_inbox_events_stay_silent_on_the_first_refresh(hass):
    coordinator, events = await _inbox_coordinator(
        hass, [_row("9400", "Out for Delivery")]
    )
    assert coordinator.last_success_time is None
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert events == []
    assert coordinator.last_success_time is not None


async def test_inbox_fires_registered_status_changed_and_delivered(hass):
    coordinator, events = await _inbox_coordinator(
        hass,
        [_row("9400", "Out for Delivery")],
        [_row("9400", "Out for Delivery"), _row("9500", "Out for Delivery")],
        [_row("9400", "Delivered"), _row("9500", "Out for Delivery")],
        [_row("9400", "Delivered"), _row("9500", "Out for Delivery")],
    )
    for _ in range(4):
        await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert events == ["registered", "delivered"]


async def test_inbox_fires_status_changed_when_not_delivered(hass):
    coordinator, events = await _inbox_coordinator(
        hass,
        [_row("9400", "Out for Delivery")],
        [_row("9400", "In Transit")],
    )
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert events == ["status_changed"]


async def test_inbox_ignores_a_parcel_first_seen_delivered(hass):
    coordinator, events = await _inbox_coordinator(
        hass,
        [_row("9400", "Out for Delivery")],
        [_row("9400", "Out for Delivery"), _row("9500", "Delivered")],
    )
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert events == []


async def test_inbox_fires_delivery_time_changed(hass):
    coordinator, events = await _inbox_coordinator(
        hass,
        [_row("9400", "Out for Delivery")],
        [_row("9400", "Out for Delivery")],
    )
    await coordinator._async_update_data()
    with patch(
        "custom_components.usps.account.coordinator.normalize_informed_delivery_parcel",
        side_effect=lambda row, **_: {
            "barcode": "9400",
            "status": "out_for_delivery",
            "delivered": False,
            "planned_from": "2026-10-25T09:00:00+00:00",
            "planned_to": "2026-10-25T17:00:00+00:00",
        },
    ):
        await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert events == ["delivery_time_changed"]


async def test_inbox_failure_keeps_the_previous_success_time(hass):
    coordinator, _ = await _inbox_coordinator(
        hass, [_row("9400", "In Transit")], USPSApiError("HTTP 500")
    )
    await coordinator._async_update_data()
    stamped = coordinator.last_success_time
    await coordinator._async_update_data()
    assert coordinator.last_success_time == stamped


async def test_inbox_events_carry_the_device_id(hass):
    coordinator, _ = await _inbox_coordinator(hass, [_row("9400", "In Transit")])
    device = MagicMock(id="device-1")
    with patch(
        "custom_components.usps.account.coordinator.dr.async_entries_for_config_entry",
        return_value=[device],
    ):
        assert coordinator._device_id() == "device-1"
        assert coordinator._device_id() == "device-1"


def _get_session(response):
    session = MagicMock()
    session.get.return_value = MagicMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    )
    return session


async def test_mail_search_hands_back_the_raw_body_and_warns_at_the_cap(caplog):
    response = AsyncMock(status=200)
    response.json = AsyncMock(
        return_value={"mailpieces": [{"mailpieceID": str(n)} for n in range(100)]}
    )
    session = MagicMock()
    session.post.return_value = MagicMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    )
    payload = await InformedDeliveryClient(
        "access", "refresh", session
    ).async_get_mail_pieces("12345678901", "2026-10-25")
    assert len(payload["mailpieces"]) == 100
    assert "truncated at the 100-piece limit" in caplog.text
    body = session.post.call_args.kwargs["json"]
    assert body["deliveryDate"] == "2026-10-25"
    assert body["maxMailPieces"] == 100


async def test_a_relative_scan_href_is_resolved_against_the_image_host():
    response = AsyncMock(status=200)
    response.read = AsyncMock(return_value=b"jpeg")
    session = _get_session(response)
    client = InformedDeliveryClient("access", "refresh", session)

    assert await client.async_get_image("/images/scan_1.jpg") == b"jpeg"
    url = session.get.call_args.args[0]
    assert url == (
        "https://apis-id.usps.com/informed-delivery-images/v2/images/scan_1.jpg"
    )

    await client.async_get_image("https://example.invalid/scan.jpg")
    assert session.get.call_args.args[0] == "https://example.invalid/scan.jpg"


async def test_a_missing_scan_is_none_rather_than_an_error(caplog):
    response = AsyncMock(status=404)
    client = InformedDeliveryClient("access", "refresh", _get_session(response))
    assert await client.async_get_image("/images/gone.jpg") is None
    assert "scan image returned HTTP 404" in caplog.text


async def test_an_expired_token_is_refreshed_once_before_refetching_a_scan(hass):
    client = MagicMock(spec=InformedDeliveryClient)
    client.access_token = "fresh"
    client.refresh_token = "rotated"
    client.refresh_generation = 0
    client.async_refresh = AsyncMock()
    client.async_get_image = AsyncMock(
        side_effect=[InformedDeliveryTokenExpired("expired"), b"jpeg"]
    )
    entry = MagicMock(spec=ConfigEntry, data={}, options={}, entry_id="e")
    coordinator = InformedDeliveryCoordinator(hass, client, entry)

    with patch.object(coordinator, "_persist_tokens") as persist:
        assert await coordinator.async_get_letter_image("/images/scan.jpg") == b"jpeg"
    client.async_refresh.assert_awaited_once()
    persist.assert_called_once()
