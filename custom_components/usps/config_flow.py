"""Config flow for the USPS parcel tracker integration."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import logging
import re
from typing import Any

import aiohttp
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .account.auth import LoginJourney, enrolled_addresses
from .account.session import async_new_informed_delivery_session
from .api.client import (
    ApiTrackingClient,
    TrackingNotEnabledError,
    USPSApiError,
    USPSAuthError,
)
from .const import (
    CONF_CONSUMER_KEY,
    CONF_CONSUMER_SECRET,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_SESSION_TOKEN,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    CONF_USERNAME,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    INFORMED_DELIVERY_COOKIE_DOCS_URL,
    SOURCE_API_TRACKING,
    SOURCE_INFORMED_DELIVERY,
)

_LOGGER = logging.getLogger(__name__)

# USPS Business Tracking issues a Consumer Key/Secret pair, exchanged for a
# client_credentials access token — not a single opaque key.
_API_SCHEMA = vol.Schema(
    {vol.Required(CONF_CONSUMER_KEY): str, vol.Required(CONF_CONSUMER_SECRET): str}
)
_INFORMED_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_SESSION_TOKEN): selector.TextSelector(
            selector.TextSelectorConfig(multiline=True)
        ),
    }
)
# Filled into the Informed Delivery form's text at runtime: hassfest forbids
# literal URLs/domains in strings.json, so the sign-in host, cookie host and
# cookie name live here and the translations only carry the placeholders.
_INFORMED_PLACEHOLDERS = {
    "login_site": "informeddelivery.usps.com",
    "cookie_site": "verified.usps.com",
    "cookie_name": "dfdc00f11bc995e",
    "docs_url": INFORMED_DELIVERY_COOKIE_DOCS_URL,
}


def normalize_tracking_code(value: str) -> str:
    """Return the tracking code upper-cased with separators stripped.

    Mirrors what a consumer site's own sanitiser does (uppercase, drop
    everything that is not ``A-Z0-9``), so codes pasted with spaces or dashes
    still work.
    """
    return re.sub(r"[^A-Z0-9]+", "", (value or "").upper())


def valid_tracking_code(value: str) -> bool:
    """Accept every non-empty code.

    Carriers' real tracking-number formats vary too much, and often aren't
    fully confirmed, to gate on a guessed shape — a false negative from a
    too-strict regex is far more annoying than a bad code that simply comes
    back "not found" on the next poll. Do not add a format regex here; this
    is a suite-wide convention, not a per-carrier TODO.
    """
    return bool(value)


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


def _clean_tracking_codes(values: list[str] | None) -> list[str]:
    """Normalise, drop blanks, and de-duplicate tracking codes."""
    codes: list[str] = []
    for value in values or []:
        code = normalize_tracking_code(value)
        if code and code not in codes:
            codes.append(code)
    return codes


class USPSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the USPS integration."""

    VERSION = 1

    # Transient Informed Delivery journey state, held only in flow-instance
    # memory across the credentials/challenge steps — never persisted. The
    # session is private (never HA's shared one — see session.py) and reused
    # across retries within one flow attempt rather than reopened each time.
    _id_journey: LoginJourney | None = None
    _id_username: str = ""
    _id_session: aiohttp.ClientSession | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> USPSOptionsFlowHandler:
        """Return the options flow handler."""
        return USPSOptionsFlowHandler()

    @callback
    def async_remove(self) -> None:
        """Close the private Informed Delivery session when this flow ends.

        Called for every terminal outcome — success, abort, or the flow
        simply being abandoned — so this is the one place that needs to
        close it, rather than every individual success/error branch above.
        """
        if self._id_session is not None:
            self.hass.async_create_task(self._id_session.close())
            self._id_session = None

    async def _validate(self, consumer_key: str, consumer_secret: str) -> None:
        """Validate the key against the live API.

        Uses the HA-managed session: this is a one-shot check, so it does not
        need a dedicated client session.
        """
        client = ApiTrackingClient(
            consumer_key, consumer_secret, async_get_clientsession(self.hass)
        )
        await client.async_validate_credentials()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the independent USPS surface for this config entry.

        USPS tracking is barcode-only — no postcode or other second factor
        pairs with a tracking code on either surface.
        """
        return self.async_show_menu(
            step_id="user", menu_options=[SOURCE_API_TRACKING, SOURCE_INFORMED_DELIVERY]
        )

    async def async_step_api_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure explicit-code API Tracking with the user's own app credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self._validate(
                    user_input[CONF_CONSUMER_KEY], user_input[CONF_CONSUMER_SECRET]
                )
            except TrackingNotEnabledError:
                _LOGGER.debug(
                    "USPS API Tracking setup: credentials valid but Tracking scope is not enabled"
                )
                errors["base"] = "tracking_not_enabled"
            except USPSAuthError as err:
                _LOGGER.debug("USPS API Tracking setup: credentials rejected (%s)", err)
                errors["base"] = "invalid_auth"
            except (USPSApiError, aiohttp.ClientError) as err:
                _LOGGER.warning(
                    "USPS API Tracking setup: could not reach the token endpoint (%s)",
                    err,
                )
                errors["base"] = "cannot_connect"
            else:
                account = hashlib.sha256(
                    user_input[CONF_CONSUMER_KEY].encode()
                ).hexdigest()[:12]
                await self.async_set_unique_id(f"{SOURCE_API_TRACKING}:{account}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="USPS API Tracking",
                    data={**dict(user_input), CONF_SOURCE: SOURCE_API_TRACKING},
                    options={
                        CONF_PARCELS: [],
                        CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                        CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                        CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                    },
                )

        return self.async_show_form(
            step_id=SOURCE_API_TRACKING, data_schema=_API_SCHEMA, errors=errors
        )

    async def _async_finish_informed_delivery(self, token_id: str) -> ConfigFlowResult:
        """Exchange a completed journey's ``tokenId`` and create/update the entry."""
        journey = self._id_journey
        if journey is None:
            raise USPSAuthError("No USPS login is in progress")
        tokens, profile = await journey.finish(token_id)
        addresses = enrolled_addresses(profile)
        if not addresses:
            # enrolled_addresses() has already logged the profile shape at WARNING.
            _LOGGER.debug(
                "USPS Informed Delivery setup: login succeeded but no usable "
                "address was found"
            )
            return self.async_show_form(
                step_id=SOURCE_INFORMED_DELIVERY,
                data_schema=_INFORMED_SCHEMA,
                errors={"base": "not_enrolled"},
                description_placeholders=_INFORMED_PLACEHOLDERS,
            )
        account = hashlib.sha256(self._id_username.lower().encode()).hexdigest()[:12]
        await self.async_set_unique_id(f"{SOURCE_INFORMED_DELIVERY}:{account}")
        data = {
            CONF_SOURCE: SOURCE_INFORMED_DELIVERY,
            CONF_USERNAME: self._id_username,
            CONF_REFRESH_TOKEN: tokens["refresh_token"],
            "access_token": tokens["access_token"],
            "addresses": addresses,
        }
        if getattr(self, "_reauth_source", None) == SOURCE_INFORMED_DELIVERY:
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="USPS Informed Delivery",
            data=data,
            options={
                CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                CONF_INCLUDE_HISTORY: False,
            },
        )

    async def async_step_informed_delivery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up (or reauthenticate) Informed Delivery from a browser session.

        USPS placed a bot gate on the interactive sign-in's credential round
        (see ``account/auth.py``), so the automatic ``LoginJourney`` can no
        longer be driven from Home Assistant — it is reached only by a real
        browser. The user signs in with their own browser, which clears that
        gate, and pastes the resulting ForgeRock session cookie here. It is
        traded once, via :meth:`_async_finish_informed_delivery`, for the
        OAuth refresh token and is never stored. The same step backs reauth,
        which is why a signed-out user whose refresh chain expired can
        recover at all.

        The interactive ``LoginJourney`` engine in ``account/auth.py`` is
        retained (it still mints the token in :meth:`LoginJourney.finish`),
        but the config-flow steps that drove its MFA challenges are gone from
        the UI. Restoring the automatic sign-in, if USPS lifts the gate, means
        bringing those steps back from git. See this repo's ``CLAUDE.md``.
        """
        errors: dict[str, str] = {}
        reauth = getattr(self, "_reauth_source", None) == SOURCE_INFORMED_DELIVERY
        if user_input is not None:
            self._id_username = user_input[CONF_USERNAME].strip()
            if self._id_session is None:
                # A genuinely private connector, not merely a private
                # session sharing HA's pooled one — see session.py for why
                # that distinction is the whole fix. Ours to close; done in
                # async_remove() below on every terminal outcome.
                self._id_session = async_new_informed_delivery_session(self.hass)
            self._id_journey = LoginJourney(self._id_session)
            try:
                return await self._async_finish_informed_delivery(
                    user_input[CONF_SESSION_TOKEN].strip()
                )
            except USPSAuthError as err:
                # A missing/expired/wrong session cookie never reaches the
                # authorization code, so finish() raises here — the actionable
                # message is "that cookie is not a live session", not "wrong
                # credentials".
                _LOGGER.debug(
                    "USPS Informed Delivery setup: session token rejected (%s)", err
                )
                errors["base"] = "invalid_session"
            except (USPSApiError, aiohttp.ClientError) as err:
                _LOGGER.warning(
                    "USPS Informed Delivery setup: could not complete the token "
                    "exchange (%s)",
                    err,
                )
                errors["base"] = "cannot_connect"
        suggested: dict[str, Any] = {}
        if reauth:
            # Prefill the username from the entry being reauthenticated: the
            # entry's unique_id is derived from it, so a user who retyped it
            # differently would trip unique_id_mismatch and never recover.
            suggested[CONF_USERNAME] = self._get_reauth_entry().data.get(
                CONF_USERNAME, ""
            )
        return self.async_show_form(
            step_id=SOURCE_INFORMED_DELIVERY,
            data_schema=self.add_suggested_values_to_schema(
                _INFORMED_SCHEMA, suggested
            ),
            errors=errors,
            description_placeholders=_INFORMED_PLACEHOLDERS,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Route reauth to the immutable source that owns this entry."""
        self._reauth_source = entry_data.get(CONF_SOURCE)
        if self._reauth_source == SOURCE_INFORMED_DELIVERY:
            return await self.async_step_informed_delivery()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a fresh API key and update the existing entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self._validate(
                    user_input[CONF_CONSUMER_KEY], user_input[CONF_CONSUMER_SECRET]
                )
            except USPSAuthError:
                errors["base"] = "invalid_auth"
            except (USPSApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates=dict(user_input)
                )

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=_API_SCHEMA, errors=errors
        )


class USPSOptionsFlowHandler(OptionsFlow):
    """Manage tracked parcels separately from integration settings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer parcel management separately from integration settings."""
        menu = ["settings"]
        if self.config_entry.data.get(CONF_SOURCE) == SOURCE_API_TRACKING:
            menu.insert(0, "parcels")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the complete tracked-code list."""
        errors: dict[str, str] = {}
        if user_input is not None:
            codes = _clean_tracking_codes(user_input.get("tracking_codes"))
            if any(not valid_tracking_code(code) for code in codes):
                errors["base"] = "invalid_tracking_code"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes],
                        CONF_DELIVERED_FILTER_TYPE: self.config_entry.options.get(
                            CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                        ),
                        CONF_DELIVERED_FILTER_AMOUNT: self.config_entry.options.get(
                            CONF_DELIVERED_FILTER_AMOUNT,
                            DEFAULT_DELIVERED_FILTER_AMOUNT,
                        ),
                        CONF_INCLUDE_HISTORY: self.config_entry.options.get(
                            CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
                        ),
                    },
                )
        current_codes = [
            p[CONF_TRACKING_CODE] for p in _current_parcels(self.config_entry)
        ]
        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id="parcels",
            data_schema=self.add_suggested_values_to_schema(
                schema, {"tracking_codes": current_codes}
            ),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the non-parcel integration settings."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_PARCELS: _current_parcels(self.config_entry),
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(user_input[CONF_INCLUDE_HISTORY]),
                },
            )
        current = self.config_entry.options
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_DELIVERED_FILTER_TYPE,
                default=current.get(
                    CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["days", "parcels"],
                    translation_key=CONF_DELIVERED_FILTER_TYPE,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_DELIVERED_FILTER_AMOUNT,
                default=current.get(
                    CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_INCLUDE_HISTORY,
                default=current.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY),
            ): selector.BooleanSelector(),
        }
        return self.async_show_form(step_id="settings", data_schema=vol.Schema(schema))
