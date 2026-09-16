"""Restricted ForgeRock/PKCE authentication helpers for Informed Delivery."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import re
import secrets
import unicodedata
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

from ..api_tracking.client import USPSApiError, USPSAuthError

_LOGGER = logging.getLogger(__name__)

AM = "https://verified.usps.com/am"
CLIENT_ID = "nh0yDbmZncxiWXiGs630"
REDIRECT_URI = "https://informeddelivery.usps.com/portal/callback"

# A real account's journey can chain more than one genuinely distinct
# interactive step (confirmed live, 2026-09-16: a ChoiceCallback — how to
# verify — followed by a ConfirmationCallback, itself possibly followed by
# an OTP entry) — this is not one MFA prompt repeated, so 2 was too tight
# and left a legitimate login stranded mid-journey with no way to continue.
# Still bounded, so a genuinely broken/looping journey gives up rather than
# running forever.
MAX_CHALLENGES = 5
# Automatic (non-interactive) round trips per journey before giving up.
MAX_AUTO_ADVANCE = 12
# Push-approval MFA (PollingWaitCallback with waitTime > 0, "check your
# phone") is polled — resubmitting the identical payload after each wait —
# for at most this many seconds total before the journey is declared broken.
PUSH_APPROVAL_TIMEOUT_SECONDS = 10.0


# USPS answers a bad password — and a bad verification code — with HTTP 200
# and a "do you want to try again?" ConfirmationCallback, not a 401. Offering
# that retry as a question just loops the journey, so it is an auth failure.
_REJECTION_PATTERN = re.compile(r"recogni[sz]e|incorrect|invalid|missing|try again", re.IGNORECASE)
_PASSWORD_CALLBACKS = {"PasswordCallback", "ValidatedCreatePasswordCallback"}
_USERNAME_CALLBACKS = {"NameCallback", "ValidatedCreateUsernameCallback"}


class JourneyChallenge(USPSAuthError):
    """A single allowlisted MFA/passcode prompt requiring user input."""

    def __init__(self, callback: dict[str, Any], prompt: str = "") -> None:
        """Keep the server callback transient; never persist it in entry data."""
        super().__init__("USPS login requires verification")
        self.callback = callback
        self.prompt = prompt


class JourneyRejected(USPSAuthError):
    """USPS turned down what was submitted and offered to start over."""


def _set_input_value(callback: dict[str, Any], value: Any) -> None:
    inputs = callback.setdefault("input", [{}])
    inputs[0]["value"] = value


def _is_answered(callback: dict[str, Any], answered_ids: set[int]) -> bool:
    """Return whether *we* already answered this exact callback object.

    Tracked by identity (``id()``), never by inspecting ``input[0].value``:
    ForgeRock ships some callback types — ``ConfirmationCallback`` observed
    live on 2026-09-16 — with a non-empty *default* value already present
    (typically ``0``, its default button index) before the user has ever
    seen the prompt. A value-presence check treats that default as "already
    answered" and silently submits it without ever asking — three identical
    rounds of that, each rejected, is exactly what produced a flat login
    failure against a real account. Only a callback we ourselves filled via
    :func:`answer_challenge` (its ``id()`` added to ``answered_ids`` by
    :meth:`LoginJourney.answer`) counts as answered.
    """
    return id(callback) in answered_ids


def _output_value(callback: dict[str, Any], name: str) -> Any:
    for item in callback.get("output", []) or []:
        if isinstance(item, dict) and item.get("name") == name:
            return item.get("value")
    return None


def _device_profile_payload(device_id: str) -> str:
    """Build the required device-profile JSON string.

    ``location`` is refused unconditionally, even though the callback's own
    ``location`` output may say ``true`` — resubmitting a location value the
    journey never asked us to collect is exactly the kind of proxying the
    allowlist exists to avoid.
    """
    return json.dumps(
        {
            "identifier": device_id,
            "metadata": {
                "hardware": {},
                "browser": {"userAgent": "Home Assistant"},
                "platform": {
                    "deviceName": "Home Assistant",
                    "platform": "Home Assistant",
                    "timezone": "America/New_York",
                },
            },
        }
    )


async def _read_json(response: aiohttp.ClientResponse, context: str) -> Any:
    """Parse a USPS JSON response, never an unhandled ``JSONDecodeError``.

    Confirmed live (2026-09-16): the Akamai edge in front of
    ``verified.usps.com`` occasionally answers a transient ``5xx`` with an
    HTML block page instead of the documented JSON error shape. That must
    surface as an ordinary :class:`USPSApiError` the config flow already
    knows how to turn into ``cannot_connect`` — not crash the request.
    """
    try:
        return await response.json(content_type=None)
    except ValueError as err:
        _LOGGER.warning(
            "USPS Informed Delivery: %s returned a non-JSON body (HTTP %s)",
            context,
            response.status,
        )
        raise USPSApiError(f"non-JSON response from {context} (HTTP {response.status})", status_code=response.status) from err


def _polling_wait_ms(callbacks: list[dict[str, Any]]) -> int | None:
    """Return the wait time of a push-approval ``PollingWaitCallback``, if any.

    ``None`` means either there is no ``PollingWaitCallback`` in this round, or
    there is one with ``waitTime`` 0 — the auto-advance case handled inline by
    :func:`fill_automatic_callbacks` instead.
    """
    for callback in callbacks:
        if callback.get("type") != "PollingWaitCallback":
            continue
        try:
            wait_ms = int(_output_value(callback, "waitTime") or 0)
        except (TypeError, ValueError):
            wait_ms = 0
        if wait_ms > 0:
            return wait_ms
    return None


def fill_automatic_callbacks(
    callbacks: list[dict[str, Any]],
    username: str,
    password: str,
    device_id: str,
    answered_ids: set[int] | None = None,
) -> None:
    """Fill only known-safe callbacks or surface one explicit challenge.

    This function is intentionally conservative: it never renders arbitrary
    ForgeRock fields into Home Assistant, preventing a changed journey from
    requesting unrelated personal information. ``answered_ids`` (identities
    of callback objects :func:`answer_challenge` has already filled — see
    :func:`_is_answered`) are skipped rather than re-raised on a resumed
    journey; a callback the server itself pre-filled with a default value is
    *not* treated as answered.
    """
    answered = answered_ids or set()
    # Only the round that also asks for a password is the credentials round.
    # A NameCallback anywhere else is USPS asking the user something — the
    # emailed verification code arrives in exactly that shape.
    credentials_round = any(cb.get("type") in _PASSWORD_CALLBACKS for cb in callbacks)
    if credentials_round and not (username and password):
        raise JourneyRejected("USPS asked for the username and password again")
    challenge: dict[str, Any] | None = None
    for callback in callbacks:
        kind = callback.get("type", "")
        if kind == "SelectIdPCallback":
            _set_input_value(callback, "localAuthentication")
        elif kind in _USERNAME_CALLBACKS and credentials_round:
            _set_input_value(callback, username)
        elif kind in _PASSWORD_CALLBACKS:
            _set_input_value(callback, password)
        elif kind in _USERNAME_CALLBACKS:
            if not _is_answered(callback, answered) and challenge is None:
                challenge = callback
        elif kind in {"TextOutputCallback", "HiddenValueCallback"}:
            continue
        elif kind == "PollingWaitCallback":
            # A non-zero waitTime (push-approval MFA) is bounded-polled by
            # LoginJourney._advance *before* this function is called — see
            # _polling_wait_ms, which this reuses so both agree on what
            # counts as "non-zero" (ForgeRock reports waitTime as a numeric
            # *string*, e.g. "0" — truthy in Python but not a real wait).
            # Reaching this branch with a real wait pending means this
            # function was called directly (e.g. a test) rather than through
            # the journey; fail safely rather than silently treat an unpolled
            # push-approval step as auto-advance.
            if _polling_wait_ms([callback]) is not None:
                raise USPSAuthError("USPS push-approval MFA requires LoginJourney polling")
            continue
        elif kind == "DeviceProfileCallback":
            _set_input_value(callback, _device_profile_payload(device_id))
        elif kind == "BooleanAttributeInputCallback":
            # Not required: leave the server's own default untouched, so an
            # optional attribute never outranks the real question beside it.
            if _output_value(callback, "required") and not _is_answered(callback, answered) and challenge is None:
                challenge = callback
        elif kind in {"TextInputCallback", "OneTimePasswordCallback", "ChoiceCallback", "ConfirmationCallback"}:
            if not _is_answered(callback, answered) and challenge is None:
                challenge = callback
        else:
            # Never seen before — logged so a changed/unfamiliar journey is
            # diagnosable from the log rather than only a generic UI error.
            # Only the callback's type and output field *names* are logged,
            # never any value (a required/consent field could carry PII).
            output_names = [
                item.get("name") for item in callback.get("output") or [] if isinstance(item, dict)
            ]
            _LOGGER.warning(
                "USPS Informed Delivery login returned an unsupported callback "
                "type %s (output fields: %s) — the login journey may have "
                "changed; please open an issue with this log line",
                kind,
                output_names,
            )
            raise USPSAuthError(f"Unsupported USPS login callback: {kind}")
    if challenge is not None:
        prompt = challenge_prompt(callbacks, challenge)
        _LOGGER.debug("USPS Informed Delivery login: pausing for a %s (prompt=%r)", challenge.get("type"), prompt)
        raise JourneyChallenge(challenge, prompt)


def _clean_text(raw: Any) -> str:
    """Server text rendered in an HA dialog: no tags, no format/bidi control characters."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(raw or "")))
    visible = "".join(char for char in text if unicodedata.category(char) not in {"Cc", "Cf"} or char.isspace())
    return " ".join(visible.split())


def _messages(callbacks: list[dict[str, Any]]) -> list[str]:
    # messageType 4 is ForgeRock's SCRIPT: JavaScript for USPS's own login
    # page, never words meant for the user.
    return [
        text
        for cb in callbacks
        if cb.get("type") == "TextOutputCallback"
        if str(_output_value(cb, "messageType")) != "4"
        if (text := _clean_text(_output_value(cb, "message")))
    ]


def challenge_prompt(callbacks: list[dict[str, Any]], callback: dict[str, Any]) -> str:
    """Return the question USPS is asking, as plain text.

    The input callback's own prompt is often blank (a ConfirmationCallback's
    is ``""``); the real question sits in a sibling TextOutputCallback.
    """
    parts = _messages(callbacks)
    own = _clean_text(_output_value(callback, "prompt"))
    if own and own not in parts:
        parts.append(own)
    return " ".join(parts)


def rejection_message(callbacks: list[dict[str, Any]]) -> str | None:
    """Return USPS's rejection text if this round is a "try again?" offer."""
    if not any(cb.get("type") == "ConfirmationCallback" for cb in callbacks):
        return None
    return next((message for message in _messages(callbacks) if _REJECTION_PATTERN.search(message)), None)


def callback_choices(callback: dict[str, Any]) -> list[str]:
    """Return the display choices for a Choice/ConfirmationCallback, if any.

    ForgeRock names the choices entry ``"choices"`` (ChoiceCallback) or
    ``"options"`` (ConfirmationCallback) — it is *not* reliably the first
    output entry, which is normally ``"prompt"`` (a string). Regression
    confirmed live, 2026-09-16: assuming index 0 held the list produced a
    zero-option ``SelectSelector`` — a challenge form with an apparently
    blank/missing field for a real account's `ConfirmationCallback`.
    """
    for item in callback.get("output") or []:
        if isinstance(item, dict) and item.get("name") in {"choices", "options"}:
            value = item.get("value")
            if isinstance(value, list):
                return [str(entry) for entry in value]
    return []


def answer_challenge(callback: dict[str, Any], answer: str) -> None:
    """Fill one allowlisted challenge without accepting arbitrary fields."""
    kind = callback.get("type", "")
    if kind in {"TextInputCallback", "OneTimePasswordCallback"} | _USERNAME_CALLBACKS:
        _set_input_value(callback, answer.strip())
        return
    if kind == "BooleanAttributeInputCallback":
        _set_input_value(callback, str(answer).strip().lower() in {"1", "true", "yes", "on"})
        return
    if kind in {"ChoiceCallback", "ConfirmationCallback"}:
        choices = callback_choices(callback)
        try:
            index = int(answer)
        except ValueError:
            index = next((item for item, choice in enumerate(choices) if choice.casefold() == answer.casefold()), -1)
        if index < 0 or index >= len(choices):
            raise USPSAuthError("Invalid USPS choice challenge answer")
        _set_input_value(callback, index)
        return
    raise USPSAuthError(f"Unsupported USPS challenge callback: {kind}")


def pkce_pair() -> tuple[str, str]:
    """Create an S256 verifier/challenge pair without logging either value."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def granted_services(address: dict[str, Any]) -> list[str]:
    """Return an address's granted service names."""
    raw = address.get("granted_services")
    return [str(item) for item in raw] if isinstance(raw, list) else []


def _compose_zip11(address: dict[str, Any]) -> str:
    parts = []
    # Each part is padded to its own width: an unpadded delivery point yields
    # a short ZIP11 that addresses a different household.
    for key, width in (("ZIPCode", 5), ("ZIPPlus4", 4), ("deliveryPoint", 2)):
        value = address.get(key)
        if value is None or str(value).strip() == "":
            return ""
        parts.append(str(value).strip().zfill(width))
    return "".join(parts)


def enrolled_addresses(profile: dict[str, Any]) -> list[dict[str, str]]:
    """Return only RMIN-enrolled address records; never expose ZIP11 as an ID."""
    raw = profile.get("addresses")
    result = []
    for address in raw if isinstance(raw, list) else []:
        if not isinstance(address, dict):
            continue
        services = granted_services(address)
        # Key names and service names only — never ZIP or street values.
        _LOGGER.debug(
            "USPS Informed Delivery: /userinfo address keys=%s granted_services=%s",
            sorted(address.keys()),
            services,
        )
        if "RMIN" not in services:
            continue
        zip11 = _compose_zip11(address)
        if len(zip11) == 11 and zip11.isdigit():
            result.append({"zip11": zip11})
        else:
            _LOGGER.warning("USPS Informed Delivery: skipping an enrolled address without a usable 11-digit ZIP")
    if not result:
        _LOGGER.debug("USPS Informed Delivery: /userinfo top-level keys=%s", sorted(profile.keys()))
    return result


async def exchange_refresh_token(session: aiohttp.ClientSession, refresh_token: str) -> dict[str, Any]:
    """Refresh a rotating token chain using the public portal client."""
    payload = {"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh_token, "redirect_uri": REDIRECT_URI}
    async with session.post(f"{AM}/oauth2/access_token", data=payload) as response:
        _LOGGER.debug("USPS Informed Delivery: POST /oauth2/access_token (refresh) -> HTTP %s", response.status)
        if response.status == 400:
            _LOGGER.warning("USPS Informed Delivery: refresh token chain expired (dead chain, HTTP 400) — reauth required")
            raise USPSAuthError("Informed Delivery refresh chain expired", status_code=400)
        if response.status != 200:
            raise USPSApiError(f"HTTP {response.status}", status_code=response.status)
        data = await _read_json(response, "/oauth2/access_token (refresh)")
    if not isinstance(data, dict) or not data.get("access_token") or not data.get("refresh_token"):
        raise USPSAuthError("invalid refresh response")
    return data


class LoginJourney:
    """A ForgeRock login journey that can pause on a challenge and resume.

    One instance lives for the lifetime of a single config-flow attempt only
    (held in the flow's own instance memory, never persisted). It never
    stores the password beyond the initial :meth:`start` call.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise a journey bound to one HTTP session."""
        self._session = session
        self._endpoint = ""
        self._headers: dict[str, str] = {}
        self._payload: dict[str, Any] = {}
        # Stable per installation: a device profile that changes on every
        # login looks like a new device to USPS and invites extra verification.
        self._device_id = hashlib.sha256(f"usps:{uuid.getnode()}".encode()).hexdigest()[:32]
        self.challenges_answered = 0
        self.pending_callback: dict[str, Any] | None = None
        self.pending_prompt = ""
        # Identities (id()) of callback objects *we* have filled via
        # answer_challenge — see _is_answered for why this can't be a
        # value-presence check.
        self._answered_ids: set[int] = set()

    async def start(self, username: str, password: str) -> str | None:
        """Begin the journey and return a ``tokenId`` once it completes."""
        async with self._session.get(f"{AM}/json/serverinfo/*") as response:
            _LOGGER.debug("USPS Informed Delivery: GET /serverinfo -> HTTP %s", response.status)
            if response.status != 200:
                raise USPSApiError(f"HTTP {response.status}", status_code=response.status)
            server = await _read_json(response, "/serverinfo")
        self._server = server
        realm = str(server.get("realm", "/alpha")).strip("/")
        self._endpoint = f"{AM}/json/realms/root/realms/{realm}/authenticate"
        self._headers = {"Accept-API-Version": "resource=2.0, protocol=1.0"}
        _LOGGER.debug(
            "USPS Informed Delivery: resolved realm=%s cookieName=%s",
            realm,
            server.get("cookieName"),
        )
        return await self._advance(username, password)

    async def answer(self, answer: str) -> str | None:
        """Answer the current pending challenge and resume the journey."""
        if self.pending_callback is None:
            raise USPSAuthError("No USPS login challenge is pending")
        if self.challenges_answered >= MAX_CHALLENGES:
            _LOGGER.warning(
                "USPS Informed Delivery login: giving up after %s challenge(s) — "
                "the journey may be stuck in a loop",
                self.challenges_answered,
            )
            raise USPSAuthError("Too many USPS login challenges")
        self.challenges_answered += 1
        _LOGGER.debug(
            "USPS Informed Delivery: answering %s (attempt %s/%s)",
            self.pending_callback.get("type") if self.pending_callback else None,
            self.challenges_answered,
            MAX_CHALLENGES,
        )
        answer_challenge(self.pending_callback, answer)
        self._answered_ids.add(id(self.pending_callback))
        self.pending_callback = None
        self.pending_prompt = ""
        return await self._advance()

    async def _advance(self, username: str | None = None, password: str | None = None) -> str | None:
        push_approval_elapsed = 0.0
        for round_number in range(MAX_AUTO_ADVANCE):
            async with self._session.post(self._endpoint, json=self._payload, headers=self._headers) as response:
                _LOGGER.debug(
                    "USPS Informed Delivery: POST /authenticate round %s -> HTTP %s",
                    round_number,
                    response.status,
                )
                if response.status == 401:
                    try:
                        error = await response.json(content_type=None)
                    except ValueError:
                        error = {}
                    _LOGGER.warning(
                        "USPS Informed Delivery login rejected the credentials "
                        "(flat HTTP 401 from /authenticate, code=%s)",
                        error.get("code") if isinstance(error, dict) else None,
                    )
                    raise USPSAuthError("Login failure", status_code=401)
                if response.status != 200:
                    # A transient Akamai/edge 5xx (often an HTML block page,
                    # not JSON) — surface it as an ordinary API error rather
                    # than attempt to parse a body that was never JSON.
                    _LOGGER.warning(
                        "USPS Informed Delivery: /authenticate round %s returned "
                        "HTTP %s (non-401) — treating as a transient failure",
                        round_number,
                        response.status,
                    )
                    raise USPSApiError(f"HTTP {response.status}", status_code=response.status)
                journey = await _read_json(response, "/authenticate")
            token_id = journey.get("tokenId") if isinstance(journey, dict) else None
            if token_id:
                _LOGGER.debug("USPS Informed Delivery: journey completed after %s round(s)", round_number + 1)
                return token_id
            callbacks = journey.get("callbacks", []) if isinstance(journey, dict) else []
            _LOGGER.debug(
                "USPS Informed Delivery: journey round %s callbacks: %s messages: %s",
                round_number,
                [c.get("type") for c in callbacks if isinstance(c, dict)],
                _messages(callbacks),
            )
            self._payload = journey
            wait_ms = _polling_wait_ms(callbacks)
            if wait_ms is not None:
                if push_approval_elapsed >= PUSH_APPROVAL_TIMEOUT_SECONDS:
                    _LOGGER.warning(
                        "USPS Informed Delivery login: push-approval MFA was not "
                        "approved within %.0fs — giving up",
                        PUSH_APPROVAL_TIMEOUT_SECONDS,
                    )
                    raise USPSAuthError("USPS push-approval MFA was not approved in time")
                delay = min(wait_ms / 1000, PUSH_APPROVAL_TIMEOUT_SECONDS - push_approval_elapsed)
                _LOGGER.debug(
                    "USPS Informed Delivery: push-approval MFA pending, waiting %.1fs (%.1fs/%.0fs elapsed)",
                    delay,
                    push_approval_elapsed,
                    PUSH_APPROVAL_TIMEOUT_SECONDS,
                )
                await asyncio.sleep(delay)
                push_approval_elapsed += delay
                # The payload is resubmitted unchanged next round — a
                # PollingWaitCallback round has nothing to fill.
                continue
            if (rejection := rejection_message(callbacks)) is not None:
                _LOGGER.debug("USPS Informed Delivery login: USPS rejected the last submission (%r)", rejection)
                raise JourneyRejected(rejection)
            try:
                fill_automatic_callbacks(callbacks, username or "", password or "", self._device_id, self._answered_ids)
            except JourneyChallenge as challenge:
                self.pending_callback = challenge.callback
                self.pending_prompt = challenge.prompt
                raise
        _LOGGER.warning(
            "USPS Informed Delivery login: journey did not reach a tokenId within %s rounds",
            MAX_AUTO_ADVANCE,
        )
        raise USPSAuthError("Login journey did not complete")

    async def finish(self, token_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Exchange the completed journey's ``tokenId`` for tokens and profile."""
        verifier, challenge = pkce_pair()
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": "openid profile informed-delivery",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "regApp": "ID",
        }
        async with self._session.get(
            f"{AM}/oauth2/authorize",
            params=params,
            cookies={str(self._server.get("cookieName")): token_id},
            allow_redirects=False,
        ) as response:
            _LOGGER.debug("USPS Informed Delivery: GET /oauth2/authorize -> HTTP %s", response.status)
            location = response.headers.get("Location", "")
        code = parse_qs(urlparse(location).query).get("code", [None])[0]
        if not code:
            _LOGGER.warning(
                "USPS Informed Delivery: /oauth2/authorize did not redirect with a "
                "code (HTTP %s) — the SSO token may have been rejected",
                response.status,
            )
            raise USPSAuthError("Authorization code missing")
        async with self._session.post(
            f"{AM}/oauth2/access_token",
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
        ) as response:
            _LOGGER.debug("USPS Informed Delivery: POST /oauth2/access_token (code exchange) -> HTTP %s", response.status)
            if response.status != 200:
                raise USPSApiError(f"HTTP {response.status}", status_code=response.status)
            tokens = await _read_json(response, "/oauth2/access_token (code exchange)")
        if not isinstance(tokens, dict) or not tokens.get("access_token") or not tokens.get("refresh_token"):
            _LOGGER.warning("USPS Informed Delivery: token exchange response was missing access/refresh tokens")
            raise USPSAuthError("Token exchange failed")
        async with self._session.get(
            f"{AM}/oauth2/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        ) as response:
            _LOGGER.debug("USPS Informed Delivery: GET /oauth2/userinfo -> HTTP %s", response.status)
            if response.status != 200:
                raise USPSApiError(f"HTTP {response.status}", status_code=response.status)
            profile = await _read_json(response, "/oauth2/userinfo")
        _LOGGER.debug(
            "USPS Informed Delivery: /userinfo returned %s address(es)",
            len(profile.get("addresses", []) or []) if isinstance(profile, dict) else 0,
        )
        return tokens, profile


async def complete_login(session: aiohttp.ClientSession, username: str, password: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run a login journey end to end, for the no-challenge happy path.

    Interactive MFA callbacks raise :class:`JourneyChallenge`: Home Assistant
    must not proxy arbitrary server prompts into a credential form. A config
    flow that needs to answer one should drive :class:`LoginJourney` directly
    instead of calling this function.
    """
    journey = LoginJourney(session)
    token_id = await journey.start(username, password)
    if not token_id:
        raise USPSAuthError("Login journey did not complete")
    return await journey.finish(token_id)
