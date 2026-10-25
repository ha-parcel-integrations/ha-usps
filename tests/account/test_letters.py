"""Parsing of the Informed Delivery letter-mail feed."""

import logging

import pytest

from custom_components.usps.account.letters import (
    extract_mail_pieces,
    merge_mail_pieces,
)


def _piece(**overrides):
    piece = {
        "mailpieceID": "MP-1",
        "imageName": "scan_1.jpg",
        "image": {
            "links": [{"href": "/images/scan_1.jpg", "title": "Mailpiece Image"}]
        },
    }
    piece.update(overrides)
    return piece


def test_a_plain_scan_becomes_a_letter():
    [letter] = extract_mail_pieces(
        {"mailpieces": [_piece()]}, requested_date="2026-10-25"
    )
    assert letter["id"] == "MP-1"
    assert letter["image_url"] == "/images/scan_1.jpg"
    assert letter["image_name"] == "scan_1"
    assert letter["date"] == "2026-10-25"
    assert letter["sender"] is None
    assert letter["campaign_mailer"] is None


def test_the_response_date_wins_over_the_requested_one():
    [letter] = extract_mail_pieces(
        {"date": "2026-10-24", "mailpieces": [_piece()]}, requested_date="2026-10-25"
    )
    assert letter["date"] == "2026-10-24"


def test_sender_prefers_the_mailpiece_field_over_the_campaign():
    [letter] = extract_mail_pieces(
        {
            "mailpieces": [
                _piece(
                    senderName="Example Bank",
                    campaign={"mailerName": "Example Marketing"},
                )
            ]
        },
        requested_date="2026-10-25",
    )
    assert letter["sender"] == "Example Bank"


def test_sender_falls_back_to_mid_then_to_the_campaign():
    [by_mid] = extract_mail_pieces(
        {"mailpieces": [_piece(MID="9012345")]}, requested_date="2026-10-25"
    )
    assert by_mid["sender"] == "9012345"

    [by_campaign] = extract_mail_pieces(
        {"mailpieces": [_piece(campaign={"mailerName": "Example Marketing"})]},
        requested_date="2026-10-25",
    )
    assert by_campaign["sender"] == "Example Marketing"


def test_a_campaign_contributes_its_ride_along_and_replacement_image():
    [letter] = extract_mail_pieces(
        {
            "mailpieces": [
                {
                    "mailpieceID": "MP-2",
                    "campaign": {
                        "mailerName": "Example Bank",
                        "replacementImage": {
                            "links": [{"href": "/images/campaign_2.png"}]
                        },
                        "rideAlong": {
                            "links": [
                                {"href": "https://example.invalid/other", "title": "X"},
                                {
                                    "href": "https://example.invalid/offer",
                                    "title": "Ride Along Target URL",
                                },
                            ]
                        },
                    },
                }
            ]
        },
        requested_date="2026-10-25",
    )
    assert letter["image_url"] == "/images/campaign_2.png"
    assert letter["campaign_mailer"] == "Example Bank"
    assert letter["campaign_url"] == "https://example.invalid/offer"


def test_the_no_mail_placeholder_is_not_a_letter(caplog):
    with caplog.at_level(logging.WARNING):
        assert (
            extract_mail_pieces(
                {"mailpieces": [{"mailpieceID": "singleBlueBox"}]},
                requested_date="2026-10-25",
            )
            == []
        )
    assert not caplog.records


def test_image_name_is_also_read_from_inside_the_image_object(caplog):
    with caplog.at_level(logging.WARNING):
        [letter] = extract_mail_pieces(
            {
                "mailpieces": [
                    {
                        "mailpieceID": "MP-3",
                        "image": {
                            "imageName": "scan_3.JPEG",
                            "links": [{"href": "/images/scan_3.jpg"}],
                        },
                    }
                ]
            },
            requested_date="2026-10-25",
        )
    assert letter["image_name"] == "scan_3"
    assert "inside its image object" in caplog.text


def test_extra_fields_are_carried_through():
    [letter] = extract_mail_pieces(
        {"mailpieces": [_piece(pieceType="Letter", IMB="00123456789")]},
        requested_date="2026-10-25",
    )
    assert letter["piece_type"] == "Letter"
    assert letter["barcode"] == "00123456789"
    assert letter["raw"]["mailpieceID"] == "MP-1"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("not a dict", "not a JSON object"),
        ({"pieces": []}, "no 'mailpieces' array"),
        ({"mailpieces": {}}, "not a list"),
    ],
)
def test_an_unexpected_response_warns_once_and_yields_nothing(
    payload, expected, caplog
):
    with caplog.at_level(logging.WARNING):
        assert extract_mail_pieces(payload, requested_date="2026-10-25") == []
        assert extract_mail_pieces(payload, requested_date="2026-10-25") == []
    assert expected in caplog.text
    assert len(caplog.records) == 1


def test_a_piece_that_is_not_an_object_is_skipped_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert (
            extract_mail_pieces(
                {"mailpieces": ["nope", _piece()]}, requested_date="2026-10-25"
            )
            != []
        )
    assert "not an object" in caplog.text


def test_a_piece_without_an_id_is_skipped_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert (
            extract_mail_pieces(
                {"mailpieces": [{"imageName": "scan.jpg"}]}, requested_date="2026-10-25"
            )
            == []
        )
    assert "without a usable 'mailpieceID'" in caplog.text


def test_a_piece_without_a_scan_is_counted_but_warned_about(caplog):
    with caplog.at_level(logging.WARNING):
        [letter] = extract_mail_pieces(
            {"mailpieces": [{"mailpieceID": "MP-4"}]}, requested_date="2026-10-25"
        )
    assert letter["image_url"] is None
    assert "no scan link" in caplog.text


def test_unknown_fields_are_reported_once(caplog):
    with caplog.at_level(logging.WARNING):
        extract_mail_pieces(
            {
                "mailpieces": [
                    _piece(somethingNew=1),
                    _piece(mailpieceID="MP-5", alsoNew=2),
                ]
            },
            requested_date="2026-10-25",
        )
    assert "does not read yet" in caplog.text
    assert len([r for r in caplog.records if "does not read yet" in r.message]) == 1


def test_merge_keeps_the_first_sighting_of_each_id():
    today = extract_mail_pieces({"mailpieces": [_piece()]}, requested_date="2026-10-25")
    yesterday = extract_mail_pieces(
        {"mailpieces": [_piece(), _piece(mailpieceID="MP-9")]},
        requested_date="2026-10-24",
    )
    merged = merge_mail_pieces([today, yesterday])
    assert [letter["id"] for letter in merged] == ["MP-1", "MP-9"]
    assert merged[0]["date"] == "2026-10-25"
