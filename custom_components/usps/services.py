"""Services for the USPS parcel tracker integration.

`usps.track_parcel` / `usps.untrack_parcel` let you add or remove a
tracked parcel without opening the integration options — so a Lovelace button
can start tracking a parcel straight from a dashboard. `usps.update_contents`
sets or clears the text describing what is in a parcel, tracked or not, on the
entry or entries that list the code.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .config_flow import normalize_tracking_code, valid_tracking_code
from .const import (
    CONF_CONTENTS,
    CONF_ENTRY_ID,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_API_TRACKING,
)
from .contents import ContentsStore, normalize_code

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"
SERVICE_UPDATE_CONTENTS = "update_contents"

_TRACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_ENTRY_ID): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
    }
)
_UNTRACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_ENTRY_ID): cv.string,
    }
)
_UPDATE_CONTENTS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
        vol.Optional(CONF_ENTRY_ID): cv.string,
    }
)


def _resolve_entry(hass: HomeAssistant, entry_id: str | None = None):
    """Resolve only an API Tracking entry; inbox entries are immutable."""
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_SOURCE) == SOURCE_API_TRACKING
    ]
    if entry_id:
        target = next(
            (
                entry
                for entry in hass.config_entries.async_entries(DOMAIN)
                if entry.entry_id == entry_id
            ),
            None,
        )
        if target is None or target.data.get(CONF_SOURCE) != SOURCE_API_TRACKING:
            raise ServiceValidationError(
                "The selected USPS entry does not support parcel management"
            )
        return target
    if len(entries) != 1:
        raise ServiceValidationError("Specify an API Tracking USPS entry")
    return entries[0]


def _loaded_coordinator(entry):
    """Return the coordinator of a set-up entry, or raise when it is not loaded."""
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError("USPS is not loaded")
    return entry.runtime_data.coordinator


def _contents_store(entry) -> ContentsStore | None:
    """Return the entry's contents store, or ``None`` when it is not loaded."""
    if entry.state is not ConfigEntryState.LOADED:
        return None
    return entry.runtime_data.coordinator.contents_store


def async_setup_services(hass: HomeAssistant, *, hub: bool = True) -> None:
    """Register the USPS services (idempotent).

    ``update_contents`` serves every entry; ``track_parcel`` and
    ``untrack_parcel`` belong to the API Tracking entry, so an Informed
    Delivery entry passes ``hub=False``.
    """

    async def _track(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        if not valid_tracking_code(tracking_code):
            raise ServiceValidationError(
                f"'{tracking_code}' is not a valid USPS tracking code"
            )
        entry = _resolve_entry(hass, call.data.get(CONF_ENTRY_ID))

        has_contents = CONF_CONTENTS in call.data
        coordinator = _loaded_coordinator(entry) if has_contents else None

        parcels = [dict(p) for p in entry.options.get(CONF_PARCELS, [])]
        if any(p[CONF_TRACKING_CODE] == tracking_code for p in parcels):
            # Already tracked: only a changed contents value does anything.
            if coordinator is not None and coordinator.contents_store.async_set(
                tracking_code, call.data[CONF_CONTENTS]
            ):
                await coordinator.async_request_refresh()
            return
        if coordinator is not None:
            # Stored before the options write, whose listener refreshes.
            coordinator.contents_store.async_set(
                tracking_code, call.data[CONF_CONTENTS]
            )
        parcels.append({CONF_TRACKING_CODE: tracking_code})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        entry = _resolve_entry(hass, call.data.get(CONF_ENTRY_ID))
        if (store := _contents_store(entry)) is not None:
            store.async_set(tracking_code, None)
        current = entry.options.get(CONF_PARCELS, [])
        kept = [p for p in current if p[CONF_TRACKING_CODE] != tracking_code]
        if len(kept) != len(current):
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_PARCELS: kept}
            )

    async def _update_contents(call: ServiceCall) -> None:
        code = normalize_code(call.data[CONF_TRACKING_CODE])
        if not code:
            raise ServiceValidationError("A tracking code is required")
        loaded = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if not loaded:
            raise ServiceValidationError("USPS is not set up")
        if entry_id := call.data.get(CONF_ENTRY_ID):
            targets = [entry for entry in loaded if entry.entry_id == entry_id]
            if not targets:
                raise ServiceValidationError(f"'{entry_id}' is not a loaded USPS entry")
        elif len(loaded) == 1:
            targets = loaded
        else:
            # A code can sit in two entries, so every entry that lists it gets
            # the value; guessing an entry would write to the wrong one.
            targets = [
                entry
                for entry in loaded
                if code in entry.runtime_data.coordinator.known_codes
            ]
            if not targets:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="update_contents_entry_required",
                )
        for entry in targets:
            coordinator = entry.runtime_data.coordinator
            if coordinator.contents_store.async_set(code, call.data.get(CONF_CONTENTS)):
                await coordinator.async_request_refresh()

    if hub and not hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        hass.services.async_register(
            DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
        )
        hass.services.async_register(
            DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_UNTRACK_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, SERVICE_UPDATE_CONTENTS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_UPDATE_CONTENTS,
            _update_contents,
            schema=_UPDATE_CONTENTS_SCHEMA,
        )


def async_unload_services(
    hass: HomeAssistant, *, hubs_left: bool, entries_left: bool
) -> None:
    """Remove the USPS services their entries no longer need.

    The hub services go with the last API Tracking entry; ``update_contents``
    stays while any entry, hub or account, is still loaded.
    """
    services = []
    if not hubs_left:
        services += [SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL]
    if not entries_left:
        services.append(SERVICE_UPDATE_CONTENTS)
    for service in services:
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
