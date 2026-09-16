"""Tests for the pausable/resumable ForgeRock login journey."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.usps.api_tracking.client import USPSApiError, USPSAuthError
from custom_components.usps.informed_delivery.auth import (
    MAX_AUTO_ADVANCE,
    MAX_CHALLENGES,
    JourneyChallenge,
    JourneyRejected,
    LoginJourney,
    complete_login,
    exchange_refresh_token,
)


def _ctx(response):
    return MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))


def _response(status=200, json=None, headers=None):
    response = MagicMock(status=status, headers=headers or {})
    response.json = AsyncMock(return_value=json)
    return response


def _session_for_happy_path():
    """A session whose journey needs exactly one OTP challenge."""
    serverinfo = _response(json={"realm": "/alpha", "cookieName": "cookie"})
    round1 = _response(json={"authId": "a1", "callbacks": [{"type": "NameCallback", "input": [{"value": ""}]}, {"type": "PasswordCallback", "input": [{"value": ""}]}]})
    round2 = _response(json={"authId": "a2", "callbacks": [{"type": "OneTimePasswordCallback", "input": [{"value": ""}]}]})
    round3 = _response(json={"tokenId": "tok"})

    session = MagicMock()
    session.get.side_effect = [
        _ctx(serverinfo),
        _ctx(_response(status=302, headers={"Location": "https://informeddelivery.usps.com/portal/callback?code=abc"})),
        _ctx(_response(json={"sub": "user", "addresses": []})),
    ]
    session.post.side_effect = [
        _ctx(round1),
        _ctx(round2),
        _ctx(round3),
        _ctx(_response(json={"access_token": "access", "refresh_token": "refresh"})),
    ]
    return session


async def test_journey_pauses_on_a_challenge_and_resumes_to_a_token():
    session = _session_for_happy_path()
    journey = LoginJourney(session)
    with pytest.raises(JourneyChallenge):
        await journey.start("user@example.test", "secret")
    assert journey.pending_callback["type"] == "OneTimePasswordCallback"
    token_id = await journey.answer("123456")
    assert token_id == "tok"
    tokens, profile = await journey.finish(token_id)
    assert tokens["access_token"] == "access"
    assert profile["sub"] == "user"


async def test_answering_without_a_pending_challenge_raises():
    journey = LoginJourney(MagicMock())
    with pytest.raises(USPSAuthError):
        await journey.answer("123456")


async def test_max_challenges_cap_trips_after_the_bound():
    session = _session_for_happy_path()
    journey = LoginJourney(session)
    with pytest.raises(JourneyChallenge):
        await journey.start("user@example.test", "secret")
    journey.challenges_answered = MAX_CHALLENGES  # already at the bound
    with pytest.raises(USPSAuthError, match="Too many"):
        await journey.answer("123456")


async def test_authorize_missing_code_raises():
    session = MagicMock()
    session.get.return_value = _ctx(_response(status=302, headers={"Location": "https://example.test/callback"}))
    journey = LoginJourney(session)
    journey._server = {"cookieName": "cookie"}
    with pytest.raises(USPSAuthError, match="Authorization code missing"):
        await journey.finish("tok")


async def test_akamai_503_mid_journey_is_a_clean_api_error():
    """Regression: an Akamai edge 5xx with a non-JSON (HTML) body, seen live
    against a real account right after the credential round, must surface as
    USPSApiError — not an unhandled JSONDecodeError."""
    session = MagicMock()
    session.get.return_value = _ctx(_response(json={"realm": "/alpha", "cookieName": "cookie"}))
    round0 = _response(json={"authId": "a1", "callbacks": [{"type": "NameCallback", "input": [{"value": ""}]}, {"type": "PasswordCallback", "input": [{"value": ""}]}]})
    round1 = MagicMock(status=503, headers={})
    round1.json = AsyncMock(side_effect=ValueError("not JSON"))
    session.post.side_effect = [_ctx(round0), _ctx(round1)]
    journey = LoginJourney(session)
    with pytest.raises(USPSApiError):
        await journey.start("user@example.test", "secret")


async def test_serverinfo_non_200_is_an_api_error():
    session = MagicMock()
    session.get.return_value = _ctx(_response(status=503))
    journey = LoginJourney(session)
    with pytest.raises(USPSApiError):
        await journey.start("user@example.test", "secret")


async def test_401_during_journey_is_a_login_failure():
    session = MagicMock()
    session.get.return_value = _ctx(_response(json={"realm": "/alpha", "cookieName": "cookie"}))
    session.post.return_value = _ctx(_response(status=401))
    journey = LoginJourney(session)
    with pytest.raises(USPSAuthError, match="Login failure"):
        await journey.start("user@example.test", "secret")


async def test_journey_that_never_completes_gives_up_after_the_bound():
    session = MagicMock()
    session.get.return_value = _ctx(_response(json={"realm": "/alpha", "cookieName": "cookie"}))
    # A journey that only ever auto-advances (never tokenId, never a challenge).
    session.post.return_value = _ctx(_response(json={"authId": "loop", "callbacks": [{"type": "HiddenValueCallback"}]}))
    journey = LoginJourney(session)
    with pytest.raises(USPSAuthError, match="did not complete"):
        await journey.start("user@example.test", "secret")
    assert session.post.call_count == MAX_AUTO_ADVANCE


async def test_finish_raises_when_token_exchange_response_is_incomplete():
    session = MagicMock()
    session.get.return_value = _ctx(_response(status=302, headers={"Location": "https://example.test/callback?code=abc"}))
    session.post.return_value = _ctx(_response(json={"access_token": "access"}))  # no refresh_token
    journey = LoginJourney(session)
    journey._server = {"cookieName": "cookie"}
    with pytest.raises(USPSAuthError, match="Token exchange failed"):
        await journey.finish("tok")


async def test_exchange_refresh_token_400_is_a_dead_chain():
    session = MagicMock()
    session.post.return_value = _ctx(_response(status=400))
    with pytest.raises(USPSAuthError, match="expired"):
        await exchange_refresh_token(session, "refresh")


async def test_exchange_refresh_token_non_200_is_an_api_error():
    session = MagicMock()
    session.post.return_value = _ctx(_response(status=500))
    with pytest.raises(USPSApiError):
        await exchange_refresh_token(session, "refresh")


async def test_exchange_refresh_token_incomplete_response_is_invalid():
    session = MagicMock()
    session.post.return_value = _ctx(_response(status=200, json={"access_token": "access"}))
    with pytest.raises(USPSAuthError, match="invalid refresh response"):
        await exchange_refresh_token(session, "refresh")


async def test_complete_login_happy_path_with_no_challenge():
    serverinfo = _response(json={"realm": "/alpha", "cookieName": "cookie"})
    round1 = _response(json={"authId": "a1", "callbacks": [{"type": "NameCallback", "input": [{"value": ""}]}, {"type": "PasswordCallback", "input": [{"value": ""}]}]})
    round2 = _response(json={"tokenId": "tok"})
    session = MagicMock()
    session.get.side_effect = [
        _ctx(serverinfo),
        _ctx(_response(status=302, headers={"Location": "https://informeddelivery.usps.com/portal/callback?code=abc"})),
        _ctx(_response(json={"sub": "user", "addresses": []})),
    ]
    session.post.side_effect = [
        _ctx(round1),
        _ctx(round2),
        _ctx(_response(json={"access_token": "access", "refresh_token": "refresh"})),
    ]
    tokens, profile = await complete_login(session, "user@example.test", "secret")
    assert tokens["access_token"] == "access"
    assert profile["sub"] == "user"


async def test_complete_login_raises_when_journey_start_yields_no_token(monkeypatch):
    fake_journey = MagicMock()
    fake_journey.start = AsyncMock(return_value="")
    with patch("custom_components.usps.informed_delivery.auth.LoginJourney", return_value=fake_journey):
        with pytest.raises(USPSAuthError, match="did not complete"):
            await complete_login(MagicMock(), "user@example.test", "secret")


async def test_push_approval_polls_and_then_completes():
    """A PollingWaitCallback with a non-zero waitTime is polled, not refused."""
    session = MagicMock()
    session.get.return_value = _ctx(_response(json={"realm": "/alpha", "cookieName": "cookie"}))
    pending = _response(json={"authId": "a1", "callbacks": [{"type": "PollingWaitCallback", "output": [{"name": "waitTime", "value": 2000}]}]})
    approved = _response(json={"tokenId": "tok"})
    session.post.side_effect = [_ctx(pending), _ctx(pending), _ctx(approved)]
    journey = LoginJourney(session)
    with patch("custom_components.usps.informed_delivery.auth.asyncio.sleep", new=AsyncMock()) as sleep:
        token_id = await journey.start("user@example.test", "secret")
    assert token_id == "tok"
    assert sleep.call_count == 2
    # The identical payload is resubmitted each round — nothing to fill.
    assert session.post.call_count == 3


async def test_string_zero_wait_time_auto_advances_without_polling():
    """Regression: a real USPS journey reports ``waitTime`` as the string
    ``"0"`` alongside a ``TextOutputCallback`` on round 0 — this must
    auto-advance immediately, not be treated as push-approval MFA."""
    session = MagicMock()
    session.get.return_value = _ctx(_response(json={"realm": "/alpha", "cookieName": "cookie"}))
    round0 = _response(json={"authId": "a1", "callbacks": [{"type": "TextOutputCallback"}, {"type": "PollingWaitCallback", "output": [{"name": "waitTime", "value": "0"}]}]})
    round1 = _response(json={"tokenId": "tok"})
    session.post.side_effect = [_ctx(round0), _ctx(round1)]
    journey = LoginJourney(session)
    with patch("custom_components.usps.informed_delivery.auth.asyncio.sleep", new=AsyncMock()) as sleep:
        token_id = await journey.start("user@example.test", "secret")
    assert token_id == "tok"
    sleep.assert_not_called()


async def test_push_approval_times_out_after_the_bound():
    session = MagicMock()
    session.get.return_value = _ctx(_response(json={"realm": "/alpha", "cookieName": "cookie"}))
    pending = _response(json={"authId": "a1", "callbacks": [{"type": "PollingWaitCallback", "output": [{"name": "waitTime", "value": 5000}]}]})
    session.post.return_value = _ctx(pending)
    journey = LoginJourney(session)
    with patch("custom_components.usps.informed_delivery.auth.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(USPSAuthError, match="not approved in time"):
            await journey.start("user@example.test", "secret")


async def test_live_email_code_journey_asks_for_the_code_not_the_username():
    """Regression mirroring a real account's journey (2026-09-16):
    credentials -> ChoiceCallback "Send Email" -> the code as a NameCallback."""
    session = MagicMock()
    session.get.return_value = _ctx(_response(json={"realm": "/alpha", "cookieName": "cookie"}))
    credentials = _response(json={"authId": "a0", "callbacks": [{"type": "NameCallback", "input": [{"value": ""}]}, {"type": "PasswordCallback", "input": [{"value": ""}]}]})
    choice = _response(json={"authId": "a1", "callbacks": [
        {"type": "TextOutputCallback", "output": [{"name": "message", "value": "How should we verify you?"}]},
        {"type": "ChoiceCallback", "output": [{"name": "prompt", "value": " "}, {"name": "choices", "value": ["Send Email p****@example.test"]}], "input": [{"value": 0}]},
    ]})
    code = _response(json={"authId": "a2", "callbacks": [
        {"type": "TextOutputCallback", "output": [{"name": "message", "value": "Enter the code we emailed you."}]},
        {"type": "NameCallback", "output": [{"name": "prompt", "value": ""}], "input": [{"value": ""}]},
        {"type": "BooleanAttributeInputCallback", "output": [{"name": "required", "value": False}], "input": [{"value": False}]},
    ]})
    done = _response(json={"tokenId": "tok"})
    session.post.side_effect = [_ctx(credentials), _ctx(choice), _ctx(code), _ctx(done)]
    journey = LoginJourney(session)
    with pytest.raises(JourneyChallenge):
        await journey.start("user@example.test", "secret")
    assert journey.pending_callback["type"] == "ChoiceCallback"
    assert journey.pending_prompt == "How should we verify you?"
    with pytest.raises(JourneyChallenge):
        await journey.answer("0")
    assert journey.pending_callback["type"] == "NameCallback"
    assert journey.pending_prompt == "Enter the code we emailed you."
    assert await journey.answer("123456") == "tok"
    submitted = session.post.call_args_list[-1].kwargs["json"]["callbacks"]
    assert submitted[1]["input"][0]["value"] == "123456"


async def test_try_again_confirmation_is_a_rejection_not_a_question():
    session = MagicMock()
    session.get.return_value = _ctx(_response(json={"realm": "/alpha", "cookieName": "cookie"}))
    session.post.return_value = _ctx(_response(json={"authId": "a1", "callbacks": [
        {"type": "TextOutputCallback", "output": [{"name": "message", "value": "The code is incorrect. Do you want to try again?"}]},
        {"type": "ConfirmationCallback", "output": [{"name": "options", "value": ["Yes", "No"]}], "input": [{"value": 0}]},
    ]}))
    journey = LoginJourney(session)
    with pytest.raises(JourneyRejected):
        await journey.start("user@example.test", "secret")
    assert journey.pending_callback is None


def test_device_identifier_is_stable_across_journeys():
    assert LoginJourney(MagicMock())._device_id == LoginJourney(MagicMock())._device_id
