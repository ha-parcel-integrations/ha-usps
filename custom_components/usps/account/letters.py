"""Pure parsing for the Informed Delivery letter-mail feed.

No I/O and no Home Assistant objects, like ``parcels.py`` next to it: letter
mail has no tracking status and no canonical parcel shape, so it never passes
through ``normalize_informed_delivery_parcel`` and lives here instead.

**This feed has never been seen from this project's own account.** Its field
names come from two independent third-party readings that disagree in places,
so every value is optional, nothing raises, and anything unexpected logs once
per Home Assistant session with enough detail to correct the parser from a
user's log rather than from another guess. That logging is the feature, not
noise — until a real response is confirmed it is the only way a wrong field
name reports itself instead of showing an empty sensor.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..api.parcels import NEW_ISSUE_URL

_LOGGER = logging.getLogger(__name__)

# USPS returns this id as a placeholder entry on a day with no mail at all.
# Counting it would report an empty mail day as one piece.
_PLACEHOLDER_IDS = frozenset({"singleBlueBox"})

_IMAGE_SUFFIX = re.compile(r"\.(?:jpe?g|png|gif|tiff?)$", re.IGNORECASE)

# Keys either source has been seen to read, plus the ones a response is known
# to carry alongside them. A key outside this set is drift worth reporting.
_KNOWN_PIECE_KEYS = frozenset(
    {
        "mailpieceID",
        "imageName",
        "image",
        "campaign",
        "senderName",
        "MID",
        "pieceType",
        "IMB",
        "encryptedPath",
        "encryptedZIP11",
        "deliveryDate",
        "date",
    }
)

_REPORT = "Please report it with this line and your debug log: " + NEW_ISSUE_URL

_logged_once: set[str] = set()


def reset_warning_state() -> None:
    """Forget which one-shot warnings have fired (tests only)."""
    _logged_once.clear()


def _warn_once(key: str, message: str, *args: Any) -> None:
    """Log ``message`` at most once per Home Assistant session."""
    if key in _logged_once:
        return
    _logged_once.add(key)
    _LOGGER.warning(message, *args)


def _links(container: Any) -> list[dict[str, Any]]:
    if not isinstance(container, dict):
        return []
    links = container.get("links")
    return (
        [link for link in links if isinstance(link, dict)]
        if isinstance(links, list)
        else []
    )


def _first_href(container: Any) -> str | None:
    """Return the first usable ``href`` from an object carrying ``links[]``."""
    for link in _links(container):
        href = link.get("href")
        if isinstance(href, str) and href:
            return href
    return None


def _ride_along_url(campaign: dict[str, Any]) -> str | None:
    """Return the campaign's click-through target, matched by link title."""
    for link in _links(campaign.get("rideAlong")):
        if link.get("title") == "Ride Along Target URL":
            href = link.get("href")
            if isinstance(href, str) and href:
                return href
    return None


def _image_name(piece: dict[str, Any], image: Any) -> str | None:
    """Return the scan's name, extension stripped.

    The two sources disagree about where this sits: on the mailpiece, or
    inside its image object. Read both rather than pick a side.
    """
    raw = piece.get("imageName")
    if not raw and isinstance(image, dict):
        raw = image.get("imageName")
        if raw:
            _warn_once(
                "image_name_nested",
                "USPS Informed Delivery: a mailpiece carries 'imageName' inside "
                "its image object rather than on the piece itself. Harmless — "
                "both are read — but worth knowing. %s",
                _REPORT,
            )
    if not isinstance(raw, str) or not raw:
        return None
    return _IMAGE_SUFFIX.sub("", raw)


def _sender(piece: dict[str, Any], campaign: dict[str, Any] | None) -> str | None:
    """Return the sender, preferring the mailpiece's own field.

    One source says a plain greyscale scan carries no structured sender and
    only branded mail has one; the other reads ``senderName`` straight off the
    mailpiece. A tester reports seeing both pieces with and without a sender,
    which fits the second reading, so it wins and the campaign name is the
    fallback.
    """
    for value in (piece.get("senderName"), piece.get("MID")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    if campaign:
        mailer = campaign.get("mailerName")
        if isinstance(mailer, str) and mailer.strip():
            return mailer.strip()
    return None


def _parse_piece(piece: dict[str, Any], *, date: str | None) -> dict[str, Any] | None:
    """Turn one raw mailpiece into a letter dict, or ``None`` to skip it."""
    piece_id = piece.get("mailpieceID")
    if isinstance(piece_id, str) and piece_id in _PLACEHOLDER_IDS:
        _LOGGER.debug(
            "USPS Informed Delivery: skipping the '%s' no-mail placeholder", piece_id
        )
        return None
    if not isinstance(piece_id, str) or not piece_id:
        _warn_once(
            "piece_without_id",
            "USPS Informed Delivery: a mailpiece came back without a usable "
            "'mailpieceID' (keys seen: %s) and was skipped — the letters sensor "
            "is undercounting. %s",
            sorted(piece),
            _REPORT,
        )
        return None

    if unknown := sorted(set(piece) - _KNOWN_PIECE_KEYS):
        _warn_once(
            "unknown_piece_keys",
            "USPS Informed Delivery: a mailpiece carries fields this "
            "integration does not read yet: %s. Nothing is broken, but they may "
            "be worth exposing. %s",
            unknown,
            _REPORT,
        )

    raw_image = piece.get("image")
    image = raw_image if isinstance(raw_image, dict) else None
    raw_campaign = piece.get("campaign")
    campaign = raw_campaign if isinstance(raw_campaign, dict) else None

    image_url = _first_href(image)
    if image_url is None and campaign is not None:
        image_url = _first_href(campaign.get("replacementImage"))
    if image_url is None:
        _warn_once(
            "piece_without_image",
            "USPS Informed Delivery: a mailpiece has no scan link (keys seen: "
            "%s) — it is still counted, but gets no image entity. %s",
            sorted(piece),
            _REPORT,
        )

    return {
        "id": piece_id,
        "sender": _sender(piece, campaign),
        "date": date,
        "image_url": image_url,
        "image_name": _image_name(piece, raw_image),
        "piece_type": piece.get("pieceType") or None,
        "barcode": piece.get("IMB") or None,
        "campaign_mailer": campaign.get("mailerName") if campaign else None,
        "campaign_url": _ride_along_url(campaign) if campaign else None,
        # The full record, like every parcel's ``raw``. Diagnostics redacts it.
        "raw": piece,
    }


def extract_mail_pieces(payload: Any, *, requested_date: str) -> list[dict[str, Any]]:
    """Extract letter entries from one ``mail-pieces/search`` response.

    ``requested_date`` is the date the call asked for; it is used when the
    response does not carry a ``date`` of its own, which the two sources
    disagree about.
    """
    if not isinstance(payload, dict):
        _warn_once(
            "payload_not_an_object",
            "USPS Informed Delivery: the mail response was %s, not a JSON "
            "object — no letters could be read. %s",
            type(payload).__name__,
            _REPORT,
        )
        return []

    pieces = payload.get("mailpieces")
    if pieces is None:
        _warn_once(
            "payload_without_mailpieces",
            "USPS Informed Delivery: the mail response carries no 'mailpieces' "
            "array (top-level keys: %s). Either there was no mail, or this "
            "integration is reading the wrong field name and the letters "
            "sensor will stay at 0. %s",
            sorted(payload),
            _REPORT,
        )
        return []
    if not isinstance(pieces, list):
        _warn_once(
            "mailpieces_not_a_list",
            "USPS Informed Delivery: 'mailpieces' was %s, not a list — no "
            "letters could be read. %s",
            type(pieces).__name__,
            _REPORT,
        )
        return []

    date = payload.get("date")
    if not isinstance(date, str) or not date:
        _LOGGER.debug(
            "USPS Informed Delivery: mail response carries no 'date'; using the "
            "requested %s",
            requested_date,
        )
        date = requested_date

    letters: list[dict[str, Any]] = []
    for piece in pieces:
        if not isinstance(piece, dict):
            _warn_once(
                "piece_not_an_object",
                "USPS Informed Delivery: a 'mailpieces' entry was %s, not an "
                "object, and was skipped. %s",
                type(piece).__name__,
                _REPORT,
            )
            continue
        if (letter := _parse_piece(piece, date=date)) is not None:
            letters.append(letter)
    return letters


def merge_mail_pieces(batches: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Union several days' letters, de-duplicated by id, first seen wins.

    USPS is queried for today and yesterday because before the morning batch
    posts, "no mail yet today" and "no mail today" are indistinguishable.
    """
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for batch in batches:
        for letter in batch:
            if letter["id"] in seen:
                continue
            seen.add(letter["id"])
            merged.append(letter)
    return merged
