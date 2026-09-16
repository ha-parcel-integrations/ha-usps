"""Informed Delivery config-flow tests."""
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.usps.const import (
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    SOURCE_INFORMED_DELIVERY,
)
from custom_components.usps.informed_delivery.auth import (
    JourneyChallenge,
    JourneyRejected,
    enrolled_addresses,
)


def test_unenrolled_profile_is_empty():
    assert enrolled_addresses({"addresses": [{"granted_services": ["MYPOST"]}]}) == []


def _rmin_profile():
    return {"addresses": [{"ZIPCode": "12345", "ZIPPlus4": "6789", "deliveryPoint": "01", "granted_services": ["RMIN"]}]}


def _fake_journey(*, challenge_callback=None, token_id="tok", tokens=None, profile=None):
    """A LoginJourney stand-in that raises once, then finishes on ``answer``."""
    journey = MagicMock()
    journey.pending_callback = challenge_callback
    journey.pending_prompt = "Enter the code we emailed you."
    if challenge_callback is not None:
        journey.start = AsyncMock(side_effect=JourneyChallenge(challenge_callback))
        journey.answer = AsyncMock(return_value=token_id)
    else:
        journey.start = AsyncMock(return_value=token_id)
    journey.finish = AsyncMock(return_value=(tokens or {"access_token": "access", "refresh_token": "refresh"}, profile or _rmin_profile()))
    return journey


async def test_config_flow_creates_only_from_rmin_profile(hass):
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    journey = _fake_journey()
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
    assert result["type"] == "create_entry"
    assert result["data"][CONF_SOURCE] == SOURCE_INFORMED_DELIVERY
    assert result["data"][CONF_REFRESH_TOKEN] == "refresh"


async def test_config_flow_aborts_without_an_enrolled_address(hass):
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    journey = _fake_journey(profile={"addresses": []})
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
    assert result["type"] == "form"
    assert result["errors"]["base"] == "not_enrolled"


async def test_passcode_challenge_round_trip(hass):
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    callback = {"type": "OneTimePasswordCallback", "input": [{"value": ""}]}
    journey = _fake_journey(challenge_callback=callback)
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
        assert result["step_id"] == "informed_delivery_challenge"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"answer": "123456"})
    assert result["type"] == "create_entry"
    journey.answer.assert_awaited_once_with("123456")


async def test_choice_challenge_is_rendered_as_a_select_selector(hass):
    from homeassistant.config_entries import SOURCE_USER
    from homeassistant.helpers import selector

    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    # Real ForgeRock shape: "prompt" (a string) is output[0], the choices
    # list is the entry named "choices" — not reliably index 0.
    callback = {"type": "ChoiceCallback", "output": [{"name": "prompt", "value": "How should we verify you?"}, {"name": "choices", "value": ["Email", "SMS"]}], "input": [{"value": None}]}
    journey = _fake_journey(challenge_callback=callback)
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
    assert isinstance(result["data_schema"].schema["answer"], selector.SelectSelector)
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"answer": "1"})
    assert result["type"] == "create_entry"
    journey.answer.assert_awaited_once_with("1")


async def test_choice_challenge_with_unrecognised_output_falls_back_to_a_text_field(hass):
    """Regression, 2026-09-16: a real ConfirmationCallback's output did not
    match the shape this integration originally assumed, which produced a
    SelectSelector with zero options — an apparently blank/missing field.
    An unrecognisable shape must fall back to a plain text field instead."""
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    callback = {"type": "ConfirmationCallback", "output": [{"value": ["OK", "Cancel"]}], "input": [{"value": None}]}
    journey = _fake_journey(challenge_callback=callback)
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
    assert result["data_schema"].schema["answer"] is str


async def test_repeated_challenge_re_shows_the_form(hass):
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    callback1 = {"type": "OneTimePasswordCallback", "input": [{"value": ""}]}
    callback2 = {"type": "TextInputCallback", "input": [{"value": ""}]}
    journey = _fake_journey(challenge_callback=callback1)
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
        journey.pending_callback = callback2
        journey.answer = AsyncMock(side_effect=JourneyChallenge(callback2))
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"answer": "123456"})
    assert result["step_id"] == "informed_delivery_challenge"
    assert result["errors"] == {}


async def test_too_many_challenges_surfaces_a_dedicated_error(hass):
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.usps.api_tracking.client import USPSAuthError
    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    callback = {"type": "OneTimePasswordCallback", "input": [{"value": ""}]}
    journey = _fake_journey(challenge_callback=callback)
    journey.answer = AsyncMock(side_effect=USPSAuthError("Too many USPS login challenges"))
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"answer": "123456"})
    assert result["errors"]["base"] == "too_many_challenges"
    assert result["step_id"] == SOURCE_INFORMED_DELIVERY


async def test_challenge_form_shows_the_usps_prompt(hass):
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    callback = {"type": "NameCallback", "input": [{"value": ""}]}
    journey = _fake_journey(challenge_callback=callback)
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
    assert result["description_placeholders"] == {"prompt": "Enter the code we emailed you."}
    assert result["data_schema"].schema["answer"] is str


async def test_rejected_code_returns_to_sign_in_with_invalid_code(hass):
    """A dead journey must never re-show a challenge with nothing pending."""
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    callback = {"type": "NameCallback", "input": [{"value": ""}]}
    journey = _fake_journey(challenge_callback=callback)
    journey.answer = AsyncMock(side_effect=JourneyRejected("The code is incorrect. Try again?"))
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"answer": "000000"})
    assert result["step_id"] == SOURCE_INFORMED_DELIVERY
    assert result["errors"]["base"] == "invalid_code"


async def test_rejected_password_is_invalid_auth(hass):
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    journey = MagicMock()
    journey.start = AsyncMock(side_effect=JourneyRejected("We do not recognize your username"))
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "wrong"})
    assert result["errors"]["base"] == "invalid_auth"


async def test_informed_delivery_start_surfaces_invalid_auth(hass):
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.usps.api_tracking.client import USPSAuthError
    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    journey = MagicMock()
    journey.start = AsyncMock(side_effect=USPSAuthError("no"))
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
    assert result["errors"]["base"] == "invalid_auth"


async def test_informed_delivery_start_surfaces_cannot_connect(hass):
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.usps.api_tracking.client import USPSApiError
    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    journey = MagicMock()
    journey.start = AsyncMock(side_effect=USPSApiError("no"))
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
    assert result["errors"]["base"] == "cannot_connect"


async def test_informed_delivery_finish_surfaces_invalid_auth(hass):
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.usps.api_tracking.client import USPSAuthError
    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    journey = _fake_journey()
    journey.finish = AsyncMock(side_effect=USPSAuthError("no"))
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
    assert result["errors"]["base"] == "invalid_auth"


async def test_informed_delivery_finish_surfaces_cannot_connect(hass):
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.usps.api_tracking.client import USPSApiError
    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    journey = _fake_journey()
    journey.finish = AsyncMock(side_effect=USPSApiError("no"))
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
    assert result["errors"]["base"] == "cannot_connect"


async def test_boolean_challenge_is_rendered_as_a_boolean_selector(hass):
    from homeassistant.config_entries import SOURCE_USER
    from homeassistant.helpers import selector

    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    callback = {"type": "BooleanAttributeInputCallback", "input": [{"value": None}]}
    journey = _fake_journey(challenge_callback=callback)
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
    assert isinstance(result["data_schema"].schema["answer"], selector.BooleanSelector)


async def test_challenge_answer_surfaces_cannot_connect(hass):
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.usps.api_tracking.client import USPSApiError
    from custom_components.usps.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
    callback = {"type": "OneTimePasswordCallback", "input": [{"value": ""}]}
    journey = _fake_journey(challenge_callback=callback)
    journey.answer = AsyncMock(side_effect=USPSApiError("no"))
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"answer": "123456"})
    assert result["errors"]["base"] == "cannot_connect"


async def test_challenge_finish_surfaces_invalid_auth_and_cannot_connect(hass):
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.usps.api_tracking.client import USPSApiError, USPSAuthError
    from custom_components.usps.const import DOMAIN

    callback = {"type": "OneTimePasswordCallback", "input": [{"value": ""}]}

    for exc in (USPSAuthError("no"), USPSApiError("no")):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": SOURCE_INFORMED_DELIVERY})
        journey = _fake_journey(challenge_callback=callback)
        journey.finish = AsyncMock(side_effect=exc)
        with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {"answer": "123456"})
        expected = "invalid_auth" if isinstance(exc, USPSAuthError) else "cannot_connect"
        assert result["errors"]["base"] == expected


async def test_reauth_keeps_the_informed_delivery_source(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.usps.const import DOMAIN

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_INFORMED_DELIVERY, "access_token": "old", CONF_REFRESH_TOKEN: "old-refresh", "addresses": [{"zip11": "12345678901"}]})
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == SOURCE_INFORMED_DELIVERY
    journey = _fake_journey()
    with patch("custom_components.usps.config_flow.LoginJourney", return_value=journey):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "user@example.test", "password": "secret"})
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_SOURCE] == SOURCE_INFORMED_DELIVERY
