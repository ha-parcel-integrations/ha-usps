"""Canonical mapping for the Informed Delivery inbox."""
from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Any

from ..api.parcels import NEW_ISSUE_URL, tracking_url
from ..const import ParcelStatus
from .client import EASTERN

_LOGGER = logging.getLogger(__name__)

# Map only capture-backed categories — deliberately narrower
# than API Tracking's status vocabulary. Reusing that broader table here would
# risk silently sorting an Informed Delivery status HA has never actually
# observed into the wrong bucket. Matched case-insensitively as a prefix
# against the stripped value: the sibling ``status`` field carries a location
# suffix (``Delivered, Front Door/Porch``).
_STATUS_PREFIXES: tuple[tuple[str, ParcelStatus], ...] = (
    ("delivered", ParcelStatus.DELIVERED),
    ("out for delivery", ParcelStatus.OUT_FOR_DELIVERY),
    ("preparing for delivery", ParcelStatus.IN_TRANSIT),
    ("on the way", ParcelStatus.IN_TRANSIT),
    # The item is still with the shipper's own partner (Amazon's network,
    # typically); USPS has not scanned it yet.
    ("usps awaiting item", ParcelStatus.REGISTERED),
)

# Kept separate from API Tracking's one-shot-warned set: the two sources map
# different vocabularies, and a code already warned about under one table
# must not suppress a legitimate warning under the other's.
_unmapped_statuses_logged: set[str] = set()


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped Informed Delivery status once per HA session."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised USPS Informed Delivery status — help us map it. Open an "
        "issue and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def map_informed_delivery_status(code: str | None) -> ParcelStatus:
    """Map an Informed Delivery ``statusCategory`` to a canonical status."""
    if not code:
        return ParcelStatus.UNKNOWN
    normalised = code.strip().lower()
    for prefix, mapped in _STATUS_PREFIXES:
        if normalised.startswith(prefix):
            return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def _eastern_day(value: Any, *, end: bool = False) -> str | None:
    if not value:
        return None
    try:
        return datetime.combine(datetime.fromisoformat(str(value)).date(), time.max if end else time.min, tzinfo=EASTERN).isoformat()
    except ValueError:
        return None


def _eastern_timestamp(value: Any) -> str | None:
    """Interpret a naive ``eventTimestamp`` as ``America/New_York`` local time.

    The package-search payload sends a bare local timestamp with no
    timezone (e.g. ``"2026-08-14T10:29:00"``) — parsed here the same way
    :func:`_eastern_day` already treats ``deliveryDate``, so both fields
    agree on what "today" means.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)
    return parsed.isoformat()


def normalize_informed_delivery_parcel(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a current-state inbox record without inventing history."""
    info = raw.get("deliveryInfo") if isinstance(raw.get("deliveryInfo"), dict) else {}
    category = info.get("statusCategory") or info.get("status")
    status = map_informed_delivery_status(category)
    delivered = status is ParcelStatus.DELIVERED
    barcode = raw.get("trackingNumber")
    delivery_date = info.get("deliveryDate") or raw.get("expectedDeliveryDate")
    return {
        "carrier": "USPS", "barcode": barcode, "sender": raw.get("shipperName") or None,
        "receiver": None, "status": status, "raw_status": info.get("status") or category,
        "delivered": delivered,
        "delivered_at": _eastern_timestamp(raw.get("eventTimestamp")) if delivered else None,
        "planned_from": None if delivered else _eastern_day(delivery_date),
        "planned_to": None if delivered else _eastern_day(delivery_date, end=True),
        "pickup": None, "pickup_point": None, "url": tracking_url(barcode),
        "weight": None, "dimensions": None, "history": None,
        # Keep the complete source record for downstream debugging and
        # compatibility. Diagnostics applies the source-specific redaction
        # list before any of this can be exported from Home Assistant.
        "raw": raw,
    }
