"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

Two things here are carrier-specific: :data:`_STATUS_PREFIXES` and
:func:`normalize_api_tracking_parcel` — this module covers the API Tracking
source only; Informed Delivery has its own narrower status map in
``informed_delivery/parcels.py``. Everything else — the timestamp parsing,
the history builder, the sort contract, the delivered filter, the one-shot
warning for unmapped statuses — is suite-wide machinery and should be left
alone.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry

from ..const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-usps/issues/new"
    "?template=unrecognised_status.yml"
)

# USPS Tracking v3 ``status``/``statusCategory`` vocabulary, provisional
# until a real credentialed response confirms it.
# Prefer mapping too little over mapping wrongly: an unmapped value surfaces
# as ``unknown`` plus a one-shot warning that asks the user to report it,
# which is how the map grows.
_STATUS_PREFIXES: tuple[tuple[str, ParcelStatus], ...] = (
    ("pre-shipment", ParcelStatus.REGISTERED),
    ("shipping label created", ParcelStatus.REGISTERED),
    ("label created", ParcelStatus.REGISTERED),
    ("accepted", ParcelStatus.IN_TRANSIT),
    ("usps in possession", ParcelStatus.IN_TRANSIT),
    ("in transit", ParcelStatus.IN_TRANSIT),
    ("arrived", ParcelStatus.IN_TRANSIT),
    ("departed", ParcelStatus.IN_TRANSIT),
    ("processed", ParcelStatus.IN_TRANSIT),
    ("preparing for delivery", ParcelStatus.IN_TRANSIT),
    ("out for delivery", ParcelStatus.OUT_FOR_DELIVERY),
    ("available for pickup", ParcelStatus.AT_PICKUP_POINT),
    ("ready for pickup", ParcelStatus.AT_PICKUP_POINT),
    ("held at post office", ParcelStatus.AT_PICKUP_POINT),
    ("delivered", ParcelStatus.DELIVERED),
    ("return to sender", ParcelStatus.RETURNING),
    ("returning", ParcelStatus.RETURNING),
    ("alert", ParcelStatus.PROBLEM),
    ("delivery exception", ParcelStatus.PROBLEM),
    ("undeliverable", ParcelStatus.PROBLEM),
    ("forwarded", ParcelStatus.PROBLEM),
)

# Status codes we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised USPS status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a carrier status code to a canonical :class:`ParcelStatus`.

    ``None`` (a not-yet-scanned parcel) reports ``unknown`` silently; an
    unrecognised code reports ``unknown`` with a one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    normalised = code.strip().lower()
    for prefix, mapped in _STATUS_PREFIXES:
        if normalised.startswith(prefix):
            return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map a history entry's status code to a canonical status, or ``None``.

    Unmapped codes keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warn once, reusing the parcel-status one-shot set.
    """
    if not code:
        return None
    normalised = code.strip().lower()
    for prefix, mapped in _STATUS_PREFIXES:
        if normalised.startswith(prefix):
            return mapped
    _warn_unmapped_status(code)
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 string for an API timestamp field.

    Numbers are treated as **epoch milliseconds** — the common case for the
    consumer APIs in this suite. Strings pass through untouched; their
    consumers are guarded by :func:`parse_iso`. Adjust the numeric branch if
    your carrier stamps in seconds.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Units contract: **centimetres**, with ``text`` pre-formatted as
    ``"L x W x H cm"`` (integer values, lowercase ``x``) so dashboards can show
    a dimension without doing their own formatting. Convert before calling if
    the carrier reports millimetres or inches.
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{int(length)} x {int(width)} x {int(height)} cm",
    }


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the carrier's event list.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. ``raw_status`` is the carrier's own text, or
    its event code when the API has no human-readable text. Sorted oldest →
    newest and capped to the most recent ``max_events``.
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        timestamp = to_iso_timestamp(event.get("eventTimestamp"))
        if not timestamp:
            continue
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(event.get("eventType") or event.get("statusCategory")),
            "raw_status": event.get("eventCode") or event.get("eventType"),
        }
        parsed = parse_iso(timestamp)
        if parsed is None:
            unparseable.append(entry)
        else:
            parseable.append((parsed, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def normalize_api_tracking_parcel(raw: dict, *, include_history: bool = False, configured_code: str | None = None) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order, and the aggregator and
    cross-carrier dashboards depend on it. A field this API does not expose
    is set to ``None`` — never omitted.

    Rules kept from the suite-wide contract:

    * ``status`` is canonical, ``raw_status`` is the carrier's own text.
    * A delivered parcel has ``delivered_at`` set and ``planned_from`` /
      ``planned_to`` cleared — the ETA is meaningless once it has arrived.
    * ``planned_to`` is ``None`` for a point estimate; only fill it when the
      carrier genuinely reports a *window*.
    * ``weight`` is kilograms, ``dimensions`` centimetres (see
      :func:`format_dimensions`).
    * ``history`` is ``None`` when the option is off — the key still exists.
    """
    tracking_code = raw.get("trackingNumber") or configured_code
    status_code = raw.get("statusCategory") or raw.get("status")
    status = map_parcel_status(status_code)
    delivered = status is ParcelStatus.DELIVERED

    events = raw.get("trackingEvents")
    delivered_events = [event for event in events or [] if isinstance(event, dict) and map_event_status(event.get("eventType") or event.get("statusCategory")) is ParcelStatus.DELIVERED]
    delivered_at = to_iso_timestamp(delivered_events[-1].get("eventTimestamp")) if delivered_events else None

    return {
        "carrier": "USPS",
        "barcode": tracking_code,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": raw.get("status") or status_code,
        "delivered": delivered,
        "delivered_at": delivered_at if delivered else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": None,
        "pickup_point": None,
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": build_history(events) if include_history else None,
        "raw": raw,
    }


# Compatibility name used by the shared coordinator.
normalize_parcel = normalize_api_tracking_parcel


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
