"""Letter mail end to end: the sensor, the scans and the announcement event."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.usps.api.client import USPSApiError
from custom_components.usps.const import (
    CONF_CONSUMER_KEY,
    CONF_CONSUMER_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    DOMAIN,
    SOURCE_API_TRACKING,
    SOURCE_INFORMED_DELIVERY,
)
from custom_components.usps.diagnostics import async_get_config_entry_diagnostics

PIECE = {
    "mailpieceID": "MP-1",
    "imageName": "scan_1.jpg",
    "senderName": "Example Bank",
    "image": {"links": [{"href": "/images/scan_1.jpg"}]},
}
SECOND_PIECE = {
    "mailpieceID": "MP-2",
    "image": {"links": [{"href": "/images/scan_2.jpg"}]},
}


def _entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE: SOURCE_INFORMED_DELIVERY,
            "access_token": "stale",
            CONF_REFRESH_TOKEN: "refresh",
            "addresses": [{"zip11": "12345678901"}],
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass, entry, *, mail, packages=None, image=b"bytes"):
    """Set the entry up with mail fetching stubbed, and return the mail mock."""
    mail_mock = (
        AsyncMock(side_effect=mail) if callable(mail) else AsyncMock(return_value=mail)
    )
    with (
        patch(
            "custom_components.usps.account.client.exchange_refresh_token",
            new=AsyncMock(
                return_value={"access_token": "fresh", "refresh_token": "rotated"}
            ),
        ),
        patch(
            "custom_components.usps.account.client.InformedDeliveryClient.async_get_packages",
            new=AsyncMock(return_value=packages or []),
        ),
        patch(
            "custom_components.usps.account.client.InformedDeliveryClient.async_get_mail_pieces",
            new=mail_mock,
        ),
        patch(
            "custom_components.usps.account.client.InformedDeliveryClient.async_get_image",
            new=AsyncMock(return_value=image),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return mail_mock


async def test_letters_sensor_counts_the_mail_and_lists_it(hass):
    entry = _entry(hass)
    mail = await _setup(hass, entry, mail={"mailpieces": [PIECE]})

    assert entry.state is ConfigEntryState.LOADED
    state = hass.states.get("sensor.usps_letters")
    assert state.state == "1"
    assert state.attributes["with_scan"] == 1
    assert state.attributes["letters"][0]["sender"] == "Example Bank"
    # Today and yesterday, so a morning batch that has not posted yet is not
    # mistaken for "no mail today".
    assert mail.await_count == 2
    assert mail.await_args_list[0].args[1] != mail.await_args_list[1].args[1]


async def test_the_same_piece_on_both_days_is_counted_once(hass):
    entry = _entry(hass)
    await _setup(hass, entry, mail={"mailpieces": [PIECE]})
    assert hass.states.get("sensor.usps_letters").state == "1"


async def test_each_scan_gets_an_image_entity(hass):
    entry = _entry(hass)
    await _setup(hass, entry, mail={"mailpieces": [PIECE, SECOND_PIECE]})

    # Named after the sender when there is one, after the id when there is not.
    assert hass.states.get("image.usps_letter_example_bank") is not None
    assert hass.states.get("image.usps_letter_mp_2") is not None


async def test_a_piece_without_a_scan_gets_no_image_entity(hass):
    entry = _entry(hass)
    await _setup(hass, entry, mail={"mailpieces": [{"mailpieceID": "MP-3"}]})

    assert hass.states.get("sensor.usps_letters").state == "1"
    assert hass.states.get("sensor.usps_letters").attributes["with_scan"] == 0
    assert hass.states.get("image.usps_letter_mp_3") is None


async def test_the_scan_is_served_through_home_assistant(hass, hass_client):
    entry = _entry(hass)
    await _setup(hass, entry, mail={"mailpieces": [PIECE]}, image=b"jpeg-bytes")

    with patch(
        "custom_components.usps.account.client.InformedDeliveryClient.async_get_image",
        new=AsyncMock(return_value=b"jpeg-bytes"),
    ) as fetch:
        client = await hass_client()
        response = await client.get("/api/image_proxy/image.usps_letter_example_bank")
        assert response.status == 200
        assert await response.read() == b"jpeg-bytes"
        # A posted scan never changes, so a second render costs no request.
        await client.get("/api/image_proxy/image.usps_letter_example_bank")
    assert fetch.await_count == 1


async def test_a_failing_scan_fetch_leaves_the_entity_alone(hass, hass_client, caplog):
    entry = _entry(hass)
    await _setup(hass, entry, mail={"mailpieces": [PIECE]})

    with patch(
        "custom_components.usps.account.client.InformedDeliveryClient.async_get_image",
        new=AsyncMock(side_effect=USPSApiError("boom")),
    ):
        client = await hass_client()
        response = await client.get("/api/image_proxy/image.usps_letter_example_bank")
    assert response.status == 500
    assert "could not fetch the scan" in caplog.text
    assert hass.states.get("image.usps_letter_example_bank") is not None


async def test_the_first_fetch_announces_nothing_and_later_mail_fires_once(hass):
    entry = _entry(hass)
    events = []

    @callback
    def _listen(event):
        events.append(event.data)

    hass.bus.async_listen(f"{DOMAIN}_letter_announced", _listen)

    await _setup(hass, entry, mail={"mailpieces": [PIECE]})
    await hass.async_block_till_done()
    assert events == []

    coordinator = entry.runtime_data.coordinator
    with (
        patch(
            "custom_components.usps.account.client.InformedDeliveryClient.async_get_packages",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "custom_components.usps.account.client.InformedDeliveryClient.async_get_mail_pieces",
            new=AsyncMock(return_value={"mailpieces": [PIECE, SECOND_PIECE]}),
        ),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert [event["id"] for event in events] == ["MP-2"]
    assert events[0]["carrier"] == "USPS"
    assert events[0]["device_id"] is not None


async def test_a_mail_failure_keeps_the_previous_list_and_the_poll(hass, caplog):
    entry = _entry(hass)
    await _setup(hass, entry, mail={"mailpieces": [PIECE]})

    coordinator = entry.runtime_data.coordinator
    with (
        patch(
            "custom_components.usps.account.client.InformedDeliveryClient.async_get_packages",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "custom_components.usps.account.client.InformedDeliveryClient.async_get_mail_pieces",
            new=AsyncMock(side_effect=USPSApiError("mail is down")),
        ),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert hass.states.get("sensor.usps_letters").state == "1"
    assert "could not fetch scanned mail" in caplog.text


async def test_a_vanished_mailpiece_loses_its_image_entity(hass):
    entry = _entry(hass)
    await _setup(hass, entry, mail={"mailpieces": [PIECE, SECOND_PIECE]})
    assert hass.states.get("image.usps_letter_mp_2") is not None

    coordinator = entry.runtime_data.coordinator
    with (
        patch(
            "custom_components.usps.account.client.InformedDeliveryClient.async_get_packages",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "custom_components.usps.account.client.InformedDeliveryClient.async_get_mail_pieces",
            new=AsyncMock(return_value={"mailpieces": [PIECE]}),
        ),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get("image.usps_letter_mp_2") is None
    assert hass.states.get("image.usps_letter_example_bank") is not None


async def test_diagnostics_redact_the_mail(hass):
    entry = _entry(hass)
    await _setup(hass, entry, mail={"mailpieces": [PIECE]})

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["counts"]["letters"] == 1
    letter = diagnostics["letters"][0]
    assert letter["id"] == "**REDACTED**"
    assert letter["sender"] == "**REDACTED**"
    assert letter["image_url"] == "**REDACTED**"
    assert letter["raw"]["senderName"] == "**REDACTED**"


async def test_an_api_tracking_entry_has_no_letters(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE: SOURCE_API_TRACKING,
            CONF_CONSUMER_KEY: "key",
            CONF_CONSUMER_SECRET: "secret",
        },
        options={"parcels": []},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.usps_letters") is None


async def test_a_scan_usps_cannot_serve_leaves_the_image_empty(hass, hass_client):
    entry = _entry(hass)
    await _setup(hass, entry, mail={"mailpieces": [PIECE]})

    with patch(
        "custom_components.usps.account.client.InformedDeliveryClient.async_get_image",
        new=AsyncMock(return_value=None),
    ):
        client = await hass_client()
        response = await client.get("/api/image_proxy/image.usps_letter_example_bank")
    assert response.status == 500
    assert hass.states.get("image.usps_letter_example_bank") is not None
