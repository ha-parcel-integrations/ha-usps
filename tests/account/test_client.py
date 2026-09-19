"""Tests for isolated Informed Delivery token handling."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.usps.account.client import (
    InformedDeliveryClient,
    InformedDeliveryTokenExpired,
)
from custom_components.usps.account.coordinator import (
    InformedDeliveryCoordinator,
)
from custom_components.usps.api.client import USPSApiError, USPSAuthError


async def test_refresh_replaces_both_rotating_tokens():
    response = AsyncMock(status=200)
    response.json = AsyncMock(return_value={"access_token": "new-access", "refresh_token": "new-refresh"})
    context = MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))
    session = MagicMock()
    session.post.return_value = context
    client = InformedDeliveryClient("old-access", "old-refresh", session)
    await client.async_refresh()
    assert client.refresh_token == "new-refresh"


async def test_100_item_inbox_cap_warns(caplog):
    response = AsyncMock(status=200)
    response.json = AsyncMock(return_value={"inboundPackages": [{"trackingNumber": str(number)} for number in range(100)]})
    session = MagicMock()
    session.post.return_value = MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))
    packages = await InformedDeliveryClient("access", "refresh", session).async_get_packages("12345678901")
    assert len(packages) == 100
    assert "may be truncated" in caplog.text


async def test_401_with_unrecognised_code_is_a_revoked_session():
    response = AsyncMock(status=401)
    response.json = AsyncMock(return_value={"code": "SOMETHING_ELSE"})
    session = MagicMock()
    session.post.return_value = MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))
    with pytest.raises(USPSAuthError, match="revoked"):
        await InformedDeliveryClient("access", "refresh", session).async_get_packages("12345678901")


async def test_401_with_unparseable_body_is_treated_as_expired():
    from custom_components.usps.account.client import (
        InformedDeliveryTokenExpired,
    )

    response = AsyncMock(status=401)
    response.json = AsyncMock(side_effect=ValueError("bad json"))
    session = MagicMock()
    session.post.return_value = MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))
    with pytest.raises(InformedDeliveryTokenExpired):
        await InformedDeliveryClient("access", "refresh", session).async_get_packages("12345678901")


async def test_non_200_non_special_status_is_an_api_error():
    response = AsyncMock(status=500)
    session = MagicMock()
    session.post.return_value = MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))
    with pytest.raises(USPSApiError):
        await InformedDeliveryClient("access", "refresh", session).async_get_packages("12345678901")


async def test_unexpected_non_dict_body_is_an_api_error():
    response = AsyncMock(status=200)
    response.json = AsyncMock(return_value=["not", "a", "dict"])
    session = MagicMock()
    session.post.return_value = MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))
    with pytest.raises(USPSApiError, match="unexpected"):
        await InformedDeliveryClient("access", "refresh", session).async_get_packages("12345678901")


async def test_empty_204_inbox_is_not_an_error():
    response = AsyncMock(status=204)
    session = MagicMock()
    session.post.return_value = MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))
    assert await InformedDeliveryClient("access", "refresh", session).async_get_packages("12345678901") == []


async def test_rate_limit_preserves_retry_after():
    response = AsyncMock(status=429, headers={"Retry-After": "30"})
    session = MagicMock()
    session.post.return_value = MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))
    with pytest.raises(USPSApiError) as err:
        await InformedDeliveryClient("access", "refresh", session).async_get_packages("12345678901")
    assert err.value.retry_after == 30


async def test_unparseable_retry_after_is_safe():
    response = AsyncMock(status=429, headers={"Retry-After": "later"})
    session = MagicMock()
    session.post.return_value = MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))
    with pytest.raises(USPSApiError) as err:
        await InformedDeliveryClient("access", "refresh", session).async_get_packages("12345678901")
    assert err.value.retry_after is None


async def test_html_edge_page_is_an_api_error():
    response = AsyncMock(status=200)
    response.json = AsyncMock(side_effect=ValueError("unexpected mimetype"))
    session = MagicMock()
    session.post.return_value = MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))
    with pytest.raises(USPSApiError, match="unparseable"):
        await InformedDeliveryClient("access", "refresh", session).async_get_packages("12345678901")


async def test_inbox_coordinator_keeps_detected_duplicate_and_marks_provenance(hass):
    entry = MagicMock(entry_id="entry", data={"addresses": [{"zip11": "12345678901"}]})
    client = AsyncMock()
    detected = {"trackingNumber": "9400", "_user_added": False, "deliveryInfo": {"statusCategory": "Out for Delivery"}}
    added = {"trackingNumber": "9400", "_user_added": True, "deliveryInfo": {"statusCategory": "Delivered"}}
    client.async_get_packages.return_value = [detected, added]
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    parcels = await coordinator._async_update_data()
    assert len(parcels) == 1
    assert parcels[0]["raw"] is detected
    assert detected["_duplicate_sources"] == ["addedPackages"]


async def test_temporary_inbox_failure_keeps_last_good_data(hass):
    entry = MagicMock(entry_id="entry", data={"addresses": [{"zip11": "12345678901"}]})
    client = AsyncMock()
    client.async_get_packages.side_effect = [[{"trackingNumber": "9400", "deliveryInfo": {"statusCategory": "Out for Delivery"}}], USPSApiError("HTTP 500")]
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    first = await coordinator._async_update_data()
    assert await coordinator._async_update_data() == first


async def test_keepalive_starts_reauth_after_dead_refresh(hass):
    entry = MagicMock(entry_id="entry", data={"source": "informed_delivery"})
    client = MagicMock()
    client.async_refresh = AsyncMock(side_effect=USPSAuthError("dead"))
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    captured = {}
    with patch("custom_components.usps.account.coordinator.async_track_time_interval", side_effect=lambda _hass, callback, _interval: captured.setdefault("callback", callback) or MagicMock()):
        coordinator.async_start_keepalive()
    with patch.object(hass.config_entries.flow, "async_init", new=AsyncMock(return_value={})) as init:
        await captured["callback"](None)
        await hass.async_block_till_done()
    assert init.called


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
    with patch("custom_components.usps.account.coordinator.async_track_time_interval", side_effect=lambda _hass, callback, _interval: captured.setdefault("callback", callback) or MagicMock()):
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
    from homeassistant.exceptions import ConfigEntryAuthFailed

    entry = MagicMock(entry_id="entry", data={"addresses": [{"zip11": "12345678901"}]})
    client = AsyncMock()
    client.refresh_generation = 1
    client.async_get_packages.side_effect = InformedDeliveryTokenExpired("expired")
    client.async_refresh.side_effect = USPSAuthError("dead chain", status_code=400)
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_auth_error_directly_raises_auth_failed(hass):
    from homeassistant.exceptions import ConfigEntryAuthFailed

    entry = MagicMock(entry_id="entry", data={"addresses": [{"zip11": "12345678901"}]})
    client = AsyncMock()
    client.async_get_packages.side_effect = USPSAuthError("revoked", status_code=401)
    coordinator = InformedDeliveryCoordinator(hass, client, entry)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_api_error_with_no_last_good_data_raises_update_failed(hass):
    from homeassistant.helpers.update_coordinator import UpdateFailed

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
    with patch("custom_components.usps.account.coordinator.async_track_time_interval", return_value=cancel) as tracker:
        assert coordinator.async_start_keepalive() is cancel
    assert tracker.call_args.args[2].total_seconds() == 600
