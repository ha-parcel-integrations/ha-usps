"""Constants for the USPS parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "usps"

# A config entry deliberately owns exactly one USPS surface.  Keep these values
# stable: they are stored in entry.data and therefore form part of the entry's
# public persistence contract.
CONF_SOURCE = "source"
SOURCE_API_TRACKING = "api_tracking"
SOURCE_INFORMED_DELIVERY = "informed_delivery"
CONF_CONSUMER_KEY = "consumer_key"
CONF_CONSUMER_SECRET = "consumer_secret"
CONF_USERNAME = "username"
CONF_REFRESH_TOKEN = "refresh_token"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Ready to collect at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping a carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Every value not listed here must come back as a literal ``None`` from
# normalize_parcel() in parcels.py (or, for "history", CONF_INCLUDE_HISTORY
# must not be wired to anything real) — never omit the key, just leave it
# empty. The docs site's carrier comparison table is generated straight from
# this constant, so drift here does not stay a local mistake, it becomes a
# wrong claim on the website.
#
#   "weight"           parcel["weight"] is ever non-null
#   "dimensions"        parcel["dimensions"] is ever non-null
#   "delivery_window"   parcel["planned_from"] and/or ["planned_to"] is ever non-null
#   "pickup_point"       parcel["pickup_point"] is ever non-null (beyond the "pickup" bool)
#   "url"                parcel["url"] is ever non-null
#   "history"            the include_history option is implemented and does something
CAPABILITIES_BY_VARIANT = {
    "Informed Delivery": frozenset({"delivery_window", "url"}),
    "API Tracking": frozenset({"history", "url"}),
}
# Legacy consumers import this flat value. New code must select from
# CAPABILITIES_BY_VARIANT using the entry source.
CAPABILITIES = frozenset().union(*CAPABILITIES_BY_VARIANT.values())

# If this carrier ever grows a second backend with a genuinely different
# payload shape (a country-specific API, not just a config option), replace
# the single CAPABILITIES above with a CAPABILITIES_BY_VARIANT dict instead:
#
#   CAPABILITIES_BY_VARIANT = {
#       "Germany": frozenset({"pickup_point", "url", "history"}),
#       "Other": frozenset({"weight", "dimensions", "delivery_window",
#                            "pickup_point", "url", "history"}),
#   }
#
# Key order is display order on the docs site's comparison table; label each
# key exactly as the carrier's own country/backend selector does. The docs
# site's generator accepts either shape — don't declare both. Do not add this
# preemptively: a single-backend carrier (the common case) keeps the flat
# CAPABILITIES above.

# USPS Business Tracking v3: a client_credentials Consumer Key/Secret pair is
# exchanged at ``TOKEN_API_URL`` for a bearer token, then presented as
# ``Authorization: Bearer <token>`` against ``TRACKING_API_URL``. Full mechanics
# live in this repo's own private research notes, not here.
TOKEN_API_URL = "https://apis.usps.com/oauth2/v3/token"
TRACKING_API_URL = "https://apis.usps.com/tracking/v3/tracking/{tracking_code}"
TRACKING_URL = "https://tools.usps.com/go/TrackConfirmAction?tLabels={tracking_code}"
INFORMED_DELIVERY_API_URL = (
    "https://apis-id.usps.com/informed-delivery/v3/dashboard/packages/search"
)

# Tracked parcels live in the config entry options as a list of
# ``{tracking_code}`` dicts — this carrier has no account or parcel feed, so the
# user enters the codes themselves. Kept as dicts so future per-parcel fields
# slot in without an options migration.
CONF_PARCELS = "parcels"
CONF_TRACKING_CODE = "tracking_code"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Dynamic, status-driven polling — unconditional across the suite, no
# user-facing interval option (see scaffold/CLAUDE.md's "Dynamic polling"
# section for the full algorithm and the reasoning behind it).
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = at least one tracked,
# not-yet-delivered parcel is out_for_delivery within HOT_LOOKAHEAD_HOURS of
# its planned_from (or has no planned_from at all); mid = anything else still
# in flight (registered, in_transit, at_pickup_point, unknown, problem,
# returning).
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

# Per-parcel status history is opt-in and off by default, identical across the
# suite. Keep it off by default even when — as here — the timeline arrives in
# the same response and costs no extra request: it is a large attribute, and on
# carriers that need a second call per parcel the cost is real.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
