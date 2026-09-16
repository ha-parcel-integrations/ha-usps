"""Tests for the isolated API Tracking OAuth client."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.usps.api_tracking.client import (
    ApiTrackingClient,
    TrackingNotEnabledError,
    USPSApiError,
    USPSAuthError,
)


def _ctx(response):
    return MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))


def _session(token: dict, parcel: dict) -> MagicMock:
    token_response = AsyncMock(status=200)
    token_response.json = AsyncMock(return_value=token)
    parcel_response = AsyncMock(status=200)
    parcel_response.json = AsyncMock(return_value=parcel)
    token_context = MagicMock(__aenter__=AsyncMock(return_value=token_response), __aexit__=AsyncMock(return_value=False))
    parcel_context = MagicMock(__aenter__=AsyncMock(return_value=parcel_response), __aexit__=AsyncMock(return_value=False))
    session = MagicMock()
    session.post.return_value = token_context
    session.get.return_value = parcel_context
    return session


async def test_credentials_are_exchanged_and_detail_is_requested():
    session = _session({"access_token": "token", "expires_in": 3600, "scope": "tracking"}, {"trackingNumber": "9400", "statusCategory": "Accepted"})
    client = ApiTrackingClient("key", "secret", session)
    assert (await client.async_get_parcel("9400"))["trackingNumber"] == "9400"
    assert session.post.call_args.kwargs["json"]["client_secret"] == "secret"
    assert session.get.call_args.kwargs["params"] == {"expand": "DETAIL"}


async def test_missing_tracking_scope_is_rejected():
    client = ApiTrackingClient("key", "secret", _session({"access_token": "token", "expires_in": 3600, "scope": "labels"}, {}))
    with pytest.raises(TrackingNotEnabledError):
        await client.async_validate_credentials()


async def test_tracking_401_refreshes_once_before_retrying():
    token_a = AsyncMock(status=200)
    token_a.json = AsyncMock(return_value={"access_token": "first", "expires_in": 3600, "scope": "tracking"})
    token_b = AsyncMock(status=200)
    token_b.json = AsyncMock(return_value={"access_token": "second", "expires_in": 3600, "scope": "tracking"})
    rejected = AsyncMock(status=401)
    ok = AsyncMock(status=200)
    ok.json = AsyncMock(return_value={"trackingNumber": "9400"})
    def context(response):
        return MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))
    session = MagicMock()
    session.post.side_effect = [context(token_a), context(token_b)]
    session.get.side_effect = [context(rejected), context(ok)]
    assert (await ApiTrackingClient("key", "secret", session).async_get_parcel("9400"))["trackingNumber"] == "9400"
    assert session.post.call_count == 2


async def test_retry_after_is_preserved_on_rate_limit():
    response = AsyncMock(status=429, headers={"Retry-After": "12"})
    session = _session({"access_token": "token", "expires_in": 3600, "scope": "tracking"}, {})
    session.get.return_value = MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))
    with pytest.raises(USPSApiError) as err:
        await ApiTrackingClient("key", "secret", session).async_get_parcel("9400")
    assert err.value.status_code == 429 and err.value.retry_after == 12


async def test_unparseable_retry_after_is_safe():
    response = AsyncMock(status=429, headers={"Retry-After": "soon"})
    session = _session({"access_token": "token", "expires_in": 3600, "scope": "tracking"}, {})
    session.get.return_value = _ctx(response)
    with pytest.raises(USPSApiError) as err:
        await ApiTrackingClient("key", "secret", session).async_get_parcel("9400")
    assert err.value.retry_after is None


async def test_cached_token_is_reused_without_a_second_token_call():
    session = _session({"access_token": "token", "expires_in": 3600, "scope": "tracking"}, {"trackingNumber": "9400"})
    client = ApiTrackingClient("key", "secret", session)
    await client.async_get_parcel("9400")
    await client.async_get_parcel("9400")
    assert session.post.call_count == 1


async def test_token_endpoint_401_is_an_auth_error():
    response = AsyncMock(status=401)
    session = MagicMock()
    session.post.return_value = _ctx(response)
    with pytest.raises(USPSAuthError):
        await ApiTrackingClient("key", "secret", session).async_validate_credentials()


async def test_token_endpoint_non_auth_error_is_a_plain_api_error():
    response = AsyncMock(status=500)
    session = MagicMock()
    session.post.return_value = _ctx(response)
    with pytest.raises(USPSApiError):
        await ApiTrackingClient("key", "secret", session).async_validate_credentials()


async def test_token_endpoint_unparseable_json_is_an_api_error():
    response = AsyncMock(status=200)
    response.json = AsyncMock(side_effect=ValueError("bad json"))
    session = MagicMock()
    session.post.return_value = _ctx(response)
    with pytest.raises(USPSApiError, match="unparseable"):
        await ApiTrackingClient("key", "secret", session).async_validate_credentials()


async def test_token_endpoint_missing_access_token_is_an_auth_error():
    response = AsyncMock(status=200)
    response.json = AsyncMock(return_value={"scope": "tracking"})
    session = MagicMock()
    session.post.return_value = _ctx(response)
    with pytest.raises(USPSAuthError):
        await ApiTrackingClient("key", "secret", session).async_validate_credentials()


async def test_token_endpoint_bad_expires_in_defaults_to_zero_lifetime():
    response = AsyncMock(status=200)
    response.json = AsyncMock(return_value={"access_token": "token", "expires_in": "not-a-number", "scope": "tracking"})
    session = MagicMock()
    session.post.return_value = _ctx(response)
    client = ApiTrackingClient("key", "secret", session)
    await client.async_validate_credentials()
    # A zero lifetime means the very next call must refresh again.
    session.post.return_value = _ctx(response)
    await client.async_validate_credentials()
    assert session.post.call_count == 2


async def test_get_parcel_second_401_gives_up_as_an_auth_error():
    session = MagicMock()
    session.post.return_value = _ctx(AsyncMock(status=200, json=AsyncMock(return_value={"access_token": "token", "expires_in": 3600, "scope": "tracking"})))
    session.get.return_value = _ctx(AsyncMock(status=401))
    with pytest.raises(USPSAuthError):
        await ApiTrackingClient("key", "secret", session).async_get_parcel("9400")


async def test_get_parcel_403_is_an_auth_error():
    session = _session({"access_token": "token", "expires_in": 3600, "scope": "tracking"}, {})
    session.get.return_value = _ctx(AsyncMock(status=403))
    with pytest.raises(USPSAuthError):
        await ApiTrackingClient("key", "secret", session).async_get_parcel("9400")


async def test_get_parcel_404_returns_none():
    session = _session({"access_token": "token", "expires_in": 3600, "scope": "tracking"}, {})
    session.get.return_value = _ctx(AsyncMock(status=404))
    assert await ApiTrackingClient("key", "secret", session).async_get_parcel("9400") is None


async def test_get_parcel_non_200_is_an_api_error():
    session = _session({"access_token": "token", "expires_in": 3600, "scope": "tracking"}, {})
    session.get.return_value = _ctx(AsyncMock(status=500))
    with pytest.raises(USPSApiError):
        await ApiTrackingClient("key", "secret", session).async_get_parcel("9400")


async def test_get_parcel_unparseable_json_is_an_api_error():
    response = AsyncMock(status=200)
    response.json = AsyncMock(side_effect=ValueError("bad json"))
    session = _session({"access_token": "token", "expires_in": 3600, "scope": "tracking"}, {})
    session.get.return_value = _ctx(response)
    with pytest.raises(USPSApiError, match="unparseable"):
        await ApiTrackingClient("key", "secret", session).async_get_parcel("9400")


async def test_get_parcel_non_dict_body_is_an_api_error():
    session = _session({"access_token": "token", "expires_in": 3600, "scope": "tracking"}, {})
    session.get.return_value = _ctx(AsyncMock(status=200, json=AsyncMock(return_value=["not", "a", "dict"])))
    with pytest.raises(USPSApiError, match="not a JSON object"):
        await ApiTrackingClient("key", "secret", session).async_get_parcel("9400")
