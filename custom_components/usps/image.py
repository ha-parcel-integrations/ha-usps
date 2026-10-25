"""Image platform for the USPS integration — scans of announced letter mail.

Informed Delivery only. The API Tracking source has no mail feed, so an entry
on that source sets up no entities here at all.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import USPSConfigEntry
from .account.coordinator import InformedDeliveryCoordinator
from .const import DOMAIN
from .device import ATTRIBUTION, build_device_info

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: USPSConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one image entity per scanned mailpiece and keep them in sync."""
    coordinator = entry.runtime_data.coordinator
    if not isinstance(coordinator, InformedDeliveryCoordinator):
        return

    # coordinator.letters is already populated: __init__.py runs the first
    # refresh before platforms are forwarded.
    unique_prefix = f"{entry.entry_id}_letter_image_"
    known_ids: set[str] = set()

    def _current_ids() -> set[str]:
        return {
            letter["id"] for letter in coordinator.letters if letter.get("image_url")
        }

    # A mailpiece that is gone from the feed keeps nothing behind: its scan is
    # no longer fetchable, so the entity is removed via the registry rather
    # than left permanently unavailable.
    def _remove(stale_ids: set[str]) -> None:
        registry = er.async_get(hass)
        for letter_id in stale_ids:
            entity_id = registry.async_get_entity_id(
                "image", DOMAIN, f"{unique_prefix}{letter_id}"
            )
            if entity_id:
                registry.async_remove(entity_id)

    initial_ids = _current_ids()
    if initial_ids:
        async_add_entities(
            USPSLetterImage(hass, coordinator, entry, letter_id)
            for letter_id in initial_ids
        )
        known_ids.update(initial_ids)

    # Sweep scans left in the registry by a previous run whose mailpieces have
    # since dropped out of the feed.
    _remove(
        {
            entity_entry.unique_id[len(unique_prefix) :]
            for entity_entry in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            if entity_entry.domain == "image"
            and entity_entry.unique_id.startswith(unique_prefix)
            and entity_entry.unique_id[len(unique_prefix) :] not in initial_ids
        }
    )

    @callback
    def _sync_letters() -> None:
        current_ids = _current_ids()
        if new_ids := current_ids - known_ids:
            async_add_entities(
                USPSLetterImage(hass, coordinator, entry, letter_id)
                for letter_id in new_ids
            )
            known_ids.update(new_ids)
        if stale_ids := known_ids - current_ids:
            _remove(stale_ids)
            known_ids.difference_update(stale_ids)

    entry.async_on_unload(coordinator.async_add_listener(_sync_letters))


class USPSLetterImage(CoordinatorEntity[InformedDeliveryCoordinator], ImageEntity):
    """The scanned exterior of one mailpiece."""

    _attr_has_entity_name = True
    _attr_translation_key = "letter_image"
    _attr_content_type = "image/jpeg"
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: InformedDeliveryCoordinator,
        entry: ConfigEntry,
        letter_id: str,
    ) -> None:
        """Initialize the image entity."""
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self._letter_id = letter_id
        self._attr_unique_id = f"{entry.entry_id}_letter_image_{letter_id}"
        self._attr_device_info = build_device_info(entry)
        self._attr_image_last_updated = dt_util.utcnow()
        self._image_url: str | None = (self._letter() or {}).get("image_url")
        self._cached_url: str | None = None
        self._cached_bytes: bytes | None = None
        self._apply_name()

    def _letter(self) -> dict[str, Any] | None:
        for letter in self.coordinator.letters:
            if letter.get("id") == self._letter_id:
                return letter
        return None

    def _apply_name(self) -> None:
        letter = self._letter() or {}
        # The sender is the only human-readable label USPS offers, and plenty
        # of pieces have none — fall back to the id so the entity is never
        # nameless.
        self._attr_translation_placeholders = {
            "letter": letter.get("sender") or self._letter_id
        }

    @property
    def available(self) -> bool:
        """Return whether the entity is available."""
        return super().available and self._letter() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        # Mirrors the per-letter fields the sensor's ``letters`` attribute and
        # the ``usps_letter_announced`` event carry, minus ``image_url`` — the
        # bytes are this entity's state.
        letter = self._letter() or {}
        return {
            "id": letter.get("id"),
            "sender": letter.get("sender"),
            "date": letter.get("date"),
            "piece_type": letter.get("piece_type"),
            "barcode": letter.get("barcode"),
            "campaign_mailer": letter.get("campaign_mailer"),
            "campaign_url": letter.get("campaign_url"),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        new_url = (self._letter() or {}).get("image_url")
        if new_url != self._image_url:
            self._image_url = new_url
            self._cached_url = None
            self._cached_bytes = None
            self._attr_image_last_updated = dt_util.utcnow()
        self._apply_name()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        """Return the current image bytes."""
        url = (self._letter() or {}).get("image_url")
        if not url:
            return None
        # A posted scan never changes, so once fetched it is cached for the
        # life of the entity.
        if self._cached_bytes is not None and self._cached_url == url:
            return self._cached_bytes
        try:
            image_bytes = await self.coordinator.async_get_letter_image(url)
        except Exception as err:  # noqa: BLE001 - a bad scan must not break the entity
            # HA's image contract does not allow async_image() to raise here,
            # so even an auth failure is deferred to the next poll, which is
            # the one place still allowed to escalate it into reauth.
            _LOGGER.warning(
                "USPS Informed Delivery: could not fetch the scan for mailpiece %s: %s",
                self._letter_id,
                err,
            )
            return None
        if image_bytes is None:
            return None
        self._cached_url = url
        self._cached_bytes = image_bytes
        return image_bytes
