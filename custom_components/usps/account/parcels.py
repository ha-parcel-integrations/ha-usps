"""Canonical mapping for the Informed Delivery inbox."""
from __future__ import annotations

import logging
import re
from datetime import datetime, time, tzinfo
from typing import Any

from ..api.parcels import NEW_ISSUE_URL, tracking_url
from ..const import ParcelStatus

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
    ("accepted", ParcelStatus.IN_TRANSIT),
    # The item is still with the shipper's own partner (Amazon's network,
    # typically); USPS has not scanned it yet.
    ("usps awaiting item", ParcelStatus.REGISTERED),
)

_TIME = r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?"
_BY_TIME = re.compile(rf"^by\s+{_TIME}$", re.IGNORECASE)
_BETWEEN_TIMES = re.compile(rf"^between\s+{_TIME}\s+and\s+{_TIME}$", re.IGNORECASE)

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


def _local_day(value: Any, tz: tzinfo, *, end: bool = False) -> str | None:
    if not value:
        return None
    try:
        return datetime.combine(datetime.fromisoformat(str(value)).date(), time.max if end else time.min, tzinfo=tz).isoformat()
    except ValueError:
        return None


def _clock(hour: str, minute: str | None, meridiem: str) -> time | None:
    h, m = int(hour), int(minute or 0)
    if not 1 <= h <= 12 or m > 59:
        return None
    return time(h % 12 + (12 if meridiem.lower() == "p" else 0), m)


def _text2_window(text: Any) -> tuple[time | None, time | None]:
    """Parse ``text2``'s ``"by 9:00pm"`` / ``"between 1:00pm and 3:00pm"``.

    Anything else, or a nonsensical time, yields no bound so the caller keeps
    the whole day.
    """
    value = str(text or "").strip()
    if match := _BY_TIME.match(value):
        return None, _clock(*match.groups())
    if match := _BETWEEN_TIMES.match(value):
        start, end = _clock(*match.groups()[:3]), _clock(*match.groups()[3:])
        if start is None or end is None or start >= end:
            return None, None
        return start, end
    return None, None


def _planned_window(value: Any, text2: Any, tz: tzinfo) -> tuple[str | None, str | None]:
    start, end = _text2_window(text2)
    if not value or (start is None and end is None):
        return _local_day(value, tz), _local_day(value, tz, end=True)
    try:
        day = datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None, None
    return (
        datetime.combine(day, start or time.min, tzinfo=tz).isoformat(),
        datetime.combine(day, end or time.max, tzinfo=tz).isoformat(),
    )


def _local_timestamp(value: Any, tz: tzinfo) -> str | None:
    """Attach ``tz`` to a naive ``eventTimestamp``."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.isoformat()


def normalize_informed_delivery_parcel(raw: dict[str, Any], *, tz: tzinfo) -> dict[str, Any]:
    """Normalise a current-state inbox record without inventing history.

    Package times are naive and local to the enrolled address, not US Eastern
    like the request date, so ``tz`` is the address's zone (in practice Home
    Assistant's own).
    """
    info = raw.get("deliveryInfo") if isinstance(raw.get("deliveryInfo"), dict) else {}
    category = info.get("statusCategory") or info.get("status")
    status = map_informed_delivery_status(category)
    delivered = status is ParcelStatus.DELIVERED
    barcode = raw.get("trackingNumber")
    delivery_date = info.get("deliveryDate") or raw.get("expectedDeliveryDate")
    planned_from, planned_to = (None, None) if delivered else _planned_window(delivery_date, info.get("text2"), tz)
    return {
        "carrier": "USPS", "barcode": barcode, "sender": raw.get("shipperName") or None,
        "receiver": None, "status": status, "raw_status": info.get("status") or category,
        "delivered": delivered,
        "delivered_at": _local_timestamp(raw.get("eventTimestamp"), tz) if delivered else None,
        "planned_from": planned_from, "planned_to": planned_to,
        "pickup": None, "pickup_point": None, "url": tracking_url(barcode),
        "weight": None, "dimensions": None, "history": None,
        # Keep the complete source record for downstream debugging and
        # compatibility. Diagnostics applies the source-specific redaction
        # list before any of this can be exported from Home Assistant.
        "raw": raw,
    }
