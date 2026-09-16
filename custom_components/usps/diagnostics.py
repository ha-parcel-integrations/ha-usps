"""Diagnostics support for the USPS parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import USPSConfigEntry

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address or a specific parcel. Over-redacting is
# cheap; under-redacting leaks a user's home address into a GitHub thread.
#
# Note: "status" (the canonical field) is deliberately *not* in this set even
# though Informed Delivery's raw deliveryInfo.status carries a drop-location
# suffix ("Delivered, Front Door/Porch") — redacting "status" globally would
# blank the primary canonical field on every parcel. That leak is covered
# instead via "raw_status" (which carries that same string) and the raw
# "status"/"text1"/"text2" fields under deliveryInfo.
TO_REDACT = {
    # Business Tracking (client_credentials) and Informed Delivery (ForgeRock)
    # credentials and tokens. client_id is a hardcoded public portal client —
    # not a secret — and is intentionally not listed here.
    "consumer_key", "consumer_secret", "refresh_token", "access_token",
    "id_token", "username", "password", "tokenId", "authId",
    "code_verifier", "code",
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "url",
    "raw_status",
    # carrier payload fields
    "trackingNumber",
    "ZIP11", "encryptedZIP11", "encryptedPath", "ZIPCode", "ZIPPlus4",
    "deliveryPoint", "secondaryAddress",
    "packageId", "packageID", "nickname", "shipperName", "eventCity",
    "eventState", "eventZIP", "firm", "originZIP", "destinationZIP",
    "recipient",
    "deliveryAddress",
    "address",
    "postalCode",
    "postal_code",
    "city",
    "street", "streetAddress",
    "email",
    "name", "display_name",
    "driver",
    "signature",
    "text1", "text2",
    "href", "image_url", "ride_along_url",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: USPSConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the USPS config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
            "skipped_from_fetch": len(coordinator.delivered_codes),
        },
        "polling": {
            "tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "suspended": coordinator.update_interval is None,
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
    }
