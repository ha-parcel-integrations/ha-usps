"""Tests for non-secret Informed Delivery auth helpers."""
import json

import pytest

from custom_components.usps.api_tracking.client import USPSAuthError
from custom_components.usps.informed_delivery.auth import (
    JourneyChallenge,
    JourneyRejected,
    answer_challenge,
    callback_choices,
    enrolled_addresses,
    fill_automatic_callbacks,
    pkce_pair,
    rejection_message,
)


def test_pkce_pair_and_rmin_filter():
    verifier, challenge = pkce_pair()
    assert verifier != challenge and len(challenge) > 20
    assert enrolled_addresses({"addresses": [{"ZIPCode": "12345", "ZIPPlus4": "6789", "deliveryPoint": "01", "granted_services": ["RMIN"]}, {"ZIPCode": "22222", "ZIPPlus4": "3333", "deliveryPoint": "44", "granted_services": ["MYPOST"]}]}) == [{"zip11": "12345678901"}]


def test_callback_allowlist_surfaces_mfa_without_rendering_unknown_fields():
    callbacks = [{"type": "NameCallback", "input": [{"value": ""}]}, {"type": "PasswordCallback", "input": [{"value": ""}]}, {"type": "OneTimePasswordCallback", "input": [{"value": ""}]}]
    with pytest.raises(JourneyChallenge):
        fill_automatic_callbacks(callbacks, "user", "secret", "device-id")
    assert callbacks[0]["input"][0]["value"] == "user"
    assert callbacks[1]["input"][0]["value"] == "secret"


def test_name_callback_outside_the_credentials_round_is_the_code_field():
    """Regression, live account 2026-09-16: after "Send Email", USPS asks for
    the emailed code as a NameCallback. Filling it with the username got the
    login rejected; it must be asked, with the sibling message as the prompt."""
    callbacks = [
        {"type": "TextOutputCallback", "output": [{"name": "message", "value": "<p>Enter the code we sent you</p>"}]},
        {"type": "NameCallback", "output": [{"name": "prompt", "value": "Code"}], "input": [{"value": ""}]},
        {"type": "BooleanAttributeInputCallback", "output": [{"name": "required", "value": False}], "input": [{"value": False}]},
    ]
    with pytest.raises(JourneyChallenge) as raised:
        fill_automatic_callbacks(callbacks, "user", "secret", "device-id")
    assert raised.value.callback is callbacks[1]
    assert raised.value.prompt == "Enter the code we sent you Code"
    assert callbacks[1]["input"][0]["value"] == ""


def test_automatic_callbacks_are_filled_even_after_the_challenge_in_list_order():
    callbacks = [
        {"type": "ChoiceCallback", "output": [{"name": "choices", "value": ["Email"]}], "input": [{"value": 0}]},
        {"type": "DeviceProfileCallback", "input": [{"value": ""}]},
    ]
    with pytest.raises(JourneyChallenge):
        fill_automatic_callbacks(callbacks, "user", "secret", "device-id")
    assert json.loads(callbacks[1]["input"][0]["value"])["identifier"] == "device-id"


def test_credentials_round_without_credentials_is_a_rejection():
    callbacks = [{"type": "NameCallback", "input": [{"value": ""}]}, {"type": "PasswordCallback", "input": [{"value": ""}]}]
    with pytest.raises(JourneyRejected):
        fill_automatic_callbacks(callbacks, "", "", "device-id")


def test_select_idp_callback_picks_local_authentication():
    callback = {"type": "SelectIdPCallback", "input": [{"value": ""}]}
    fill_automatic_callbacks([callback], "user", "secret", "device-id")
    assert callback["input"][0]["value"] == "localAuthentication"


def test_rejection_message_needs_a_confirmation_and_a_rejection_text():
    message = {"type": "TextOutputCallback", "output": [{"name": "message", "value": "We do not recognize your username and/or password. Do you want to try again?"}]}
    confirmation = {"type": "ConfirmationCallback", "output": [{"name": "options", "value": ["Yes", "No"]}]}
    assert rejection_message([message, confirmation]).startswith("We do not recognize")
    assert rejection_message([message]) is None
    neutral = {"type": "TextOutputCallback", "output": [{"name": "message", "value": "Send a code to your email?"}]}
    assert rejection_message([neutral, confirmation]) is None


def test_name_callback_answer_is_the_trimmed_code():
    callback = {"type": "NameCallback", "input": [{"value": ""}]}
    answer_challenge(callback, " 123456 ")
    assert callback["input"][0]["value"] == "123456"


# Real ForgeRock shape: output[0] is "prompt" (a string); the choices list
# is the entry named "choices" (ChoiceCallback) or "options"
# (ConfirmationCallback) — never reliably index 0. Regression, 2026-09-16:
# assuming index 0 held the list produced a zero-option, apparently blank
# SelectSelector against a real account's ConfirmationCallback.
def test_choice_challenge_submits_server_choice_index():
    callback = {"type": "ChoiceCallback", "output": [{"name": "prompt", "value": "How?"}, {"name": "choices", "value": ["Email", "SMS"]}], "input": [{"value": None}]}
    answer_challenge(callback, "SMS")
    assert callback["input"][0]["value"] == 1


def test_choice_challenge_accepts_a_bare_index():
    callback = {"type": "ChoiceCallback", "output": [{"name": "prompt", "value": "How?"}, {"name": "choices", "value": ["Email", "SMS"]}], "input": [{"value": None}]}
    answer_challenge(callback, "0")
    assert callback["input"][0]["value"] == 0


def test_choice_challenge_rejects_an_out_of_range_answer():
    callback = {"type": "ChoiceCallback", "output": [{"name": "prompt", "value": "How?"}, {"name": "choices", "value": ["Email", "SMS"]}], "input": [{"value": None}]}
    with pytest.raises(USPSAuthError):
        answer_challenge(callback, "nonsense")


def test_choice_callback_with_unrecognised_output_shape_has_no_choices():
    """Regression, 2026-09-16: a live account's real ConfirmationCallback did
    not match the shape this integration originally assumed — this must
    degrade to an empty choice list (and the config flow falls back to a
    text field), never crash or silently pick an arbitrary option."""
    callback = {"type": "ChoiceCallback", "output": [{"value": ["Email", "SMS"]}], "input": [{"value": None}]}
    assert callback_choices(callback) == []


def test_confirmation_callback_uses_the_same_choice_shape():
    callback = {"type": "ConfirmationCallback", "output": [{"name": "prompt", "value": "Verify email?"}, {"name": "options", "value": ["OK", "Cancel"]}], "input": [{"value": None}]}
    answer_challenge(callback, "OK")
    assert callback["input"][0]["value"] == 0


def test_device_profile_callback_gets_structured_json_never_a_location():
    callback = {"type": "DeviceProfileCallback", "output": [{"name": "location", "value": True}], "input": [{"value": ""}]}
    fill_automatic_callbacks([callback], "user", "secret", "device-id")
    payload = json.loads(callback["input"][0]["value"])
    assert payload["identifier"] == "device-id"
    assert "location" not in payload
    assert payload["metadata"]["platform"]["timezone"] == "America/New_York"


def test_boolean_callback_not_required_auto_advances_without_touching_input():
    callback = {"type": "BooleanAttributeInputCallback", "output": [{"name": "required", "value": False}], "input": [{"value": None}]}
    fill_automatic_callbacks([callback], "user", "secret", "device-id")
    assert callback["input"][0]["value"] is None


def test_boolean_callback_required_raises_a_challenge():
    callback = {"type": "BooleanAttributeInputCallback", "output": [{"name": "required", "value": True}], "input": [{"value": None}]}
    with pytest.raises(JourneyChallenge):
        fill_automatic_callbacks([callback], "user", "secret", "device-id")


def test_boolean_callback_required_with_a_default_value_still_raises_a_challenge():
    """A pre-filled default (server-supplied, not ours) must still be asked."""
    callback = {"type": "BooleanAttributeInputCallback", "output": [{"name": "required", "value": True}], "input": [{"value": False}]}
    with pytest.raises(JourneyChallenge):
        fill_automatic_callbacks([callback], "user", "secret", "device-id")


def test_boolean_callback_answer_is_parsed_as_a_real_boolean():
    callback = {"type": "BooleanAttributeInputCallback", "input": [{"value": None}]}
    answer_challenge(callback, "true")
    assert callback["input"][0]["value"] is True
    answer_challenge(callback, "false")
    assert callback["input"][0]["value"] is False


def test_already_answered_challenge_is_not_re_raised_on_resume():
    """A resumed journey re-fills the whole callback list; a callback whose
    id() is in answered_ids (LoginJourney tracks the ones it filled itself)
    must not trip the challenge a second time."""
    callbacks = [{"type": "OneTimePasswordCallback", "input": [{"value": "123456"}]}]
    fill_automatic_callbacks(callbacks, "user", "secret", "device-id", {id(callbacks[0])})


def test_a_server_provided_default_value_does_not_count_as_answered():
    """Regression, live account, 2026-09-16: ForgeRock's ConfirmationCallback
    ships a non-empty default value (its default button index, ``0`` here)
    before the user has ever seen the prompt. Checking "is input non-empty"
    instead of "did *we* fill this" silently auto-submitted that default
    three rounds running and got the login rejected outright — it must
    still raise a challenge."""
    callback = {"type": "ConfirmationCallback", "output": [{"value": ["OK", "Cancel"]}], "input": [{"value": 0}]}
    with pytest.raises(JourneyChallenge):
        fill_automatic_callbacks([callback], "user", "secret", "device-id")


def test_polling_wait_callback_auto_advances_when_wait_time_is_zero():
    callback = {"type": "PollingWaitCallback", "output": [{"name": "waitTime", "value": 0}]}
    fill_automatic_callbacks([callback], "user", "secret", "device-id")


def test_polling_wait_callback_with_wait_time_is_refused():
    callback = {"type": "PollingWaitCallback", "output": [{"name": "waitTime", "value": 5000}]}
    with pytest.raises(USPSAuthError):
        fill_automatic_callbacks([callback], "user", "secret", "device-id")


def test_polling_wait_callback_auto_advances_when_wait_time_is_the_string_zero():
    """ForgeRock reports ``waitTime`` as a numeric *string* in practice.

    ``"0"`` is truthy in Python, so a naive ``if output_value:`` check would
    wrongly treat a real account's zero-wait auto-advance step as push-approval
    MFA — this was caught live against a real USPS account on 2026-09-16.
    """
    callback = {"type": "PollingWaitCallback", "output": [{"name": "waitTime", "value": "0"}]}
    fill_automatic_callbacks([callback], "user", "secret", "device-id")


def test_polling_wait_callback_with_string_wait_time_is_refused():
    callback = {"type": "PollingWaitCallback", "output": [{"name": "waitTime", "value": "2000"}]}
    with pytest.raises(USPSAuthError):
        fill_automatic_callbacks([callback], "user", "secret", "device-id")


def test_unknown_callback_is_refused():
    callback = {"type": "TermsAndConditionsCallback", "input": [{"value": ""}]}
    with pytest.raises(USPSAuthError):
        fill_automatic_callbacks([callback], "user", "secret", "device-id")


def test_answer_challenge_refuses_an_unsupported_callback_kind():
    with pytest.raises(USPSAuthError):
        answer_challenge({"type": "TermsAndConditionsCallback"}, "yes")


def test_script_text_output_is_never_shown_as_the_prompt():
    """Regression, live 2026-09-16: USPS ships page JavaScript as a
    messageType 4 TextOutputCallback beside the passcode NameCallback."""
    callbacks = [
        {"type": "TextOutputCallback", "output": [{"name": "message", "value": "var COUNT = 10; function go(obs) {}"}, {"name": "messageType", "value": "4"}]},
        {"type": "NameCallback", "output": [{"name": "prompt", "value": "Passcode"}], "input": [{"value": ""}]},
    ]
    with pytest.raises(JourneyChallenge) as raised:
        fill_automatic_callbacks(callbacks, "user", "secret", "device-id")
    assert raised.value.prompt == "Passcode"


def test_enrolled_addresses_pads_each_zip_part_to_its_width():
    profile = {"addresses": [
        {"ZIPCode": "2345", "ZIPPlus4": 6789, "deliveryPoint": 1, "granted_services": ["RMIN", "MYPOST"]},
        {"ZIPCode": "22222", "ZIPPlus4": "3333", "deliveryPoint": "44", "granted_services": ["MYPOST"]},
    ]}
    assert enrolled_addresses(profile) == [{"zip11": "02345678901"}]


def test_enrolled_addresses_skips_unusable_zip_and_malformed_profiles():
    assert enrolled_addresses({"addresses": [{"ZIPCode": "12345", "granted_services": ["RMIN"]}]}) == []
    assert enrolled_addresses({"addresses": "nope"}) == []
    assert enrolled_addresses({"addresses": [{"ZIPCode": "12345", "granted_services": "RMIN"}]}) == []
