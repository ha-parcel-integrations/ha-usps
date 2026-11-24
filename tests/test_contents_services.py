"""Tests for the contents action, the track_parcel field and the store lifecycle."""

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.usps.account.coordinator import InformedDeliveryCoordinator
from custom_components.usps.api import coordinator as module
from custom_components.usps.api.coordinator import USPSCoordinator
from custom_components.usps.const import (
    CONF_CONSUMER_KEY,
    CONF_CONSUMER_SECRET,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_API_TRACKING,
    SOURCE_INFORMED_DELIVERY,
)
from custom_components.usps.services import async_setup_services

API_CODE = "9400111899223197428490"
OTHER_API_CODE = "9400111899223197428491"
ID_CODE = "9400111899223197428499"
ID_DELIVERED_CODE = "9400111899223197428498"
UNTRACKED = "9400111899223197428000"
API_GET = "custom_components.usps.api.client.ApiTrackingClient.async_get_parcel"
ID_PACKAGES = (
    "custom_components.usps.account.client.InformedDeliveryClient.async_get_packages"
)
ID_MAIL = (
    "custom_components.usps.account.client.InformedDeliveryClient.async_get_mail_pieces"
)
ID_REFRESH = "custom_components.usps.account.client.exchange_refresh_token"
STORE_KEY = f"{DOMAIN}.contents.%s"


async def _api_payload(code):
    return {"trackingNumber": code, "statusCategory": "In Transit"}


def _packages():
    return [
        {
            "trackingNumber": ID_CODE,
            "deliveryInfo": {
                "statusCategory": "Out for Delivery",
                "deliveryDate": "2099-01-02",
            },
        },
        {
            "trackingNumber": ID_DELIVERED_CODE,
            "deliveryInfo": {"statusCategory": "Delivered"},
        },
    ]


@contextmanager
def _polls(packages=None):
    with (
        patch(API_GET, new=AsyncMock(side_effect=_api_payload)),
        patch(
            ID_REFRESH,
            new=AsyncMock(return_value={"access_token": "a", "refresh_token": "r"}),
        ),
        patch(ID_MAIL, new=AsyncMock(return_value={"mailpieces": []})),
        patch(
            ID_PACKAGES,
            new=AsyncMock(return_value=_packages() if packages is None else packages),
        ),
    ):
        yield


async def _setup(hass, codes=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE: SOURCE_API_TRACKING,
            CONF_CONSUMER_KEY: "key",
            CONF_CONSUMER_SECRET: "secret",
        },
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes or []]},
    )
    entry.add_to_hass(hass)
    with _polls():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _setup_id(hass, packages=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE: SOURCE_INFORMED_DELIVERY,
            "access_token": "access",
            CONF_REFRESH_TOKEN: "refresh",
            "addresses": [{"zip11": "12345678901"}],
        },
    )
    entry.add_to_hass(hass)
    with _polls(packages):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _call(hass, service: str, data: dict) -> None:
    with _polls():
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
        await hass.async_block_till_done()
        # A refresh requested inside the debouncer's cooldown runs after it.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()


def _contents(entry, code):
    parcels = {
        p["barcode"]: p
        for p in [
            *entry.runtime_data.coordinator.data,
            *entry.runtime_data.coordinator.delivered,
        ]
    }
    return parcels[code]["contents"]


def _store(entry):
    return entry.runtime_data.coordinator.contents_store


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    entry = await _setup(hass, [API_CODE])
    assert _contents(entry, API_CODE) is None


async def test_update_contents_sets_replaces_and_clears(hass):
    entry = await _setup(hass, [API_CODE])

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: API_CODE, "contents": " Running  shoes "},
    )
    assert _contents(entry, API_CODE) == "Running shoes"

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: API_CODE, "contents": "Winter socks"},
    )
    assert _contents(entry, API_CODE) == "Winter socks"

    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: API_CODE, "contents": "  "}
    )
    assert _contents(entry, API_CODE) is None


async def test_update_contents_without_text_clears(hass):
    entry = await _setup(hass, [API_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: API_CODE, "contents": "Running shoes"},
    )

    await _call(hass, "update_contents", {CONF_TRACKING_CODE: API_CODE})

    assert _contents(entry, API_CODE) is None


async def test_update_contents_normalizes_the_code(hass):
    entry = await _setup(hass, ["abc123"])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: " abc123 ", "contents": "Running shoes"},
    )
    assert _store(entry).get("ABC123") == "Running shoes"


async def test_update_contents_works_for_a_code_that_is_not_tracked(hass):
    entry = await _setup(hass, [API_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )

    assert _store(entry).get(UNTRACKED) == "Running shoes"
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: API_CODE}]


async def test_update_contents_refreshes_only_when_something_changed(hass):
    await _setup(hass, [API_CODE])
    data = {CONF_TRACKING_CODE: API_CODE, "contents": "Running shoes"}
    with patch.object(USPSCoordinator, "async_request_refresh", AsyncMock()) as r:
        await _call(hass, "update_contents", data)
        assert r.await_count == 1
        await _call(hass, "update_contents", data)
        assert r.await_count == 1
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: UNTRACKED})
        assert r.await_count == 1


async def test_an_unchanged_value_writes_nothing(hass):
    entry = await _setup(hass, [API_CODE])
    store = _store(entry)
    with patch.object(store._store, "async_delay_save") as save:
        assert store.async_set(API_CODE, "Running shoes") is True
        assert save.call_count == 1
        assert store.async_set(API_CODE, " Running shoes ") is False
        assert store.async_set(UNTRACKED, None) is False
        assert save.call_count == 1


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: " "})


async def test_update_contents_needs_a_loaded_entry(hass):
    async_setup_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "update_contents", {CONF_TRACKING_CODE: API_CODE}, blocking=True
        )


async def test_update_contents_ignores_an_entry_that_is_not_loaded(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: API_CODE, "contents": "Running shoes"},
        )


async def test_track_parcel_stores_contents_for_a_new_code(hass):
    entry = await _setup(hass)
    await _call(
        hass,
        "track_parcel",
        {CONF_TRACKING_CODE: API_CODE, "contents": "Running shoes"},
    )

    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: API_CODE}]
    assert _contents(entry, API_CODE) == "Running shoes"


async def test_track_parcel_without_contents_is_unchanged(hass):
    entry = await _setup(hass)
    await _call(hass, "track_parcel", {CONF_TRACKING_CODE: API_CODE})

    assert _store(entry).get(API_CODE) is None
    assert _contents(entry, API_CODE) is None


async def test_track_parcel_replaces_and_clears_contents_of_a_tracked_code(hass):
    entry = await _setup(hass, [API_CODE])
    track = {CONF_TRACKING_CODE: API_CODE}

    await _call(hass, "track_parcel", {**track, "contents": "Running shoes"})
    assert _contents(entry, API_CODE) == "Running shoes"

    await _call(hass, "track_parcel", {**track, "contents": "Winter socks"})
    assert _contents(entry, API_CODE) == "Winter socks"

    await _call(hass, "track_parcel", {**track, "contents": ""})
    assert _contents(entry, API_CODE) is None
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: API_CODE}]


async def test_track_parcel_unchanged_contents_requests_no_refresh(hass):
    await _setup(hass, [API_CODE])
    data = {CONF_TRACKING_CODE: API_CODE, "contents": "Running shoes"}
    with patch.object(USPSCoordinator, "async_request_refresh", AsyncMock()) as r:
        await _call(hass, "track_parcel", data)
        await _call(hass, "track_parcel", data)
    assert r.await_count == 1


async def test_track_parcel_with_contents_needs_a_loaded_hub(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "track_parcel",
            {CONF_TRACKING_CODE: API_CODE, "contents": "Running shoes"},
        )


async def test_untrack_parcel_drops_the_contents(hass):
    entry = await _setup(hass, [API_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: API_CODE, "contents": "Running shoes"},
    )

    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: API_CODE})

    assert _store(entry).get(API_CODE) is None
    assert entry.options[CONF_PARCELS] == []


async def test_untrack_parcel_skips_a_hub_that_is_not_loaded(hass):
    entry = await _setup(hass, [API_CODE])
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)

    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: API_CODE})

    assert entry.options[CONF_PARCELS] == []


async def test_a_stored_value_survives_a_restart_of_the_entry(hass, hass_storage):
    entry = await _setup(hass, [API_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: API_CODE, "contents": "Running shoes"},
    )
    await _store(entry)._store.async_save(_store(entry)._data_to_save())

    assert await hass.config_entries.async_unload(entry.entry_id)
    with _polls():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert _contents(entry, API_CODE) == "Running shoes"


async def test_an_entry_without_a_store_file_sets_up_and_loads_empty(
    hass, hass_storage
):
    assert not [key for key in hass_storage if key.startswith(f"{DOMAIN}.contents.")]
    entry = await _setup(hass, [API_CODE])
    assert entry.state is ConfigEntryState.LOADED
    assert _contents(entry, API_CODE) is None


@pytest.mark.parametrize("kind", ["api", "informed_delivery"])
async def test_removing_the_entry_removes_its_store(hass, hass_storage, kind):
    if kind == "api":
        entry, code = await _setup(hass, [API_CODE]), API_CODE
    else:
        entry, code = await _setup_id(hass), ID_CODE
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: code, "contents": "Running shoes"},
    )
    store = _store(entry)
    await store._store.async_save(store._data_to_save())
    key = STORE_KEY % entry.entry_id
    assert key in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_a_contents_change_alone_fires_no_event(hass):
    await _setup(hass, [API_CODE])
    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", events.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", events.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", events.append)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: API_CODE, "contents": "Running shoes"},
    )

    assert events == []


# ---------------------------------------------------------------------------
# overlay on the coordinators
# ---------------------------------------------------------------------------


async def test_user_contents_is_keyed_on_the_requested_code(hass):
    """A payload carrying another number still finds the value stored for the code."""
    entry = await _setup(hass, [API_CODE])
    coordinator = entry.runtime_data.coordinator
    coordinator.contents_store.async_set(API_CODE, "Running shoes")

    with patch(API_GET, new=AsyncMock(return_value=await _api_payload(UNTRACKED))):
        data = await coordinator._async_update_data()

    assert data[0]["barcode"] == UNTRACKED
    assert data[0]["contents"] == "Running shoes"


async def test_user_contents_overlays_the_carrier_value(hass):
    entry = await _setup(hass, [API_CODE])
    coordinator = entry.runtime_data.coordinator
    real = module.normalize_parcel

    def carrier_text(raw, **kwargs):
        parcel = real(raw, **kwargs)
        parcel["contents"] = "Carrier text"
        return parcel

    with (
        patch.object(module, "normalize_parcel", side_effect=carrier_text),
        patch(API_GET, new=AsyncMock(return_value=await _api_payload(API_CODE))),
    ):
        assert (await coordinator._async_update_data())[0]["contents"] == (
            "Carrier text"
        )
        coordinator.contents_store.async_set(API_CODE, "Running shoes")
        assert (await coordinator._async_update_data())[0]["contents"] == (
            "Running shoes"
        )
        coordinator.contents_store.async_set(API_CODE, None)
        assert (await coordinator._async_update_data())[0]["contents"] == (
            "Carrier text"
        )


async def test_informed_delivery_overlay_covers_active_and_delivered(hass):
    entry = await _setup_id(hass)
    coordinator = entry.runtime_data.coordinator
    assert isinstance(coordinator, InformedDeliveryCoordinator)
    coordinator.contents_store.async_set(ID_CODE, "Running shoes")
    coordinator.contents_store.async_set(ID_DELIVERED_CODE, "Winter socks")

    with _polls():
        active = await coordinator._async_update_data()

    assert active[0]["contents"] == "Running shoes"
    assert coordinator.delivered[0]["contents"] == "Winter socks"


async def test_informed_delivery_row_without_a_code_gets_no_contents(hass):
    entry = await _setup_id(hass, [{"deliveryInfo": {"statusCategory": "In Transit"}}])
    parcel = entry.runtime_data.coordinator.data[0]
    assert parcel["barcode"] is None
    assert parcel["contents"] is None
    assert entry.runtime_data.coordinator.known_codes == frozenset()


async def test_known_codes_list_every_code_the_entry_holds(hass):
    api = await _setup(hass, [API_CODE])
    informed = await _setup_id(hass)

    assert api.runtime_data.coordinator.known_codes == {API_CODE}
    assert informed.runtime_data.coordinator.known_codes == {
        ID_CODE,
        ID_DELIVERED_CODE,
    }


async def test_informed_delivery_entries_take_contents_too(hass):
    informed = await _setup_id(hass)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ID_CODE, "contents": "Running shoes"},
    )

    assert _contents(informed, ID_CODE) == "Running shoes"


# ---------------------------------------------------------------------------
# several entries
# ---------------------------------------------------------------------------


async def test_update_contents_goes_to_the_entry_that_lists_the_code(hass):
    api = await _setup(hass, [API_CODE])
    informed = await _setup_id(hass)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: API_CODE, "contents": "Running shoes"},
    )

    assert _store(api).get(API_CODE) == "Running shoes"
    assert _store(informed).get(API_CODE) is None


async def test_update_contents_goes_to_every_entry_that_lists_the_code(hass):
    api = await _setup(hass, [ID_CODE])
    informed = await _setup_id(hass)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ID_CODE, "contents": "Running shoes"},
    )

    assert _store(api).get(ID_CODE) == "Running shoes"
    assert _store(informed).get(ID_CODE) == "Running shoes"


async def test_update_contents_goes_to_every_informed_delivery_entry(hass):
    home = await _setup_id(hass)
    work = await _setup_id(hass)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ID_CODE, "contents": "Running shoes"},
    )

    assert _store(home).get(ID_CODE) == "Running shoes"
    assert _store(work).get(ID_CODE) == "Running shoes"


async def test_a_single_loaded_entry_is_targeted_directly(hass):
    informed = await _setup_id(hass)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )

    assert _store(informed).get(UNTRACKED) == "Running shoes"


async def test_update_contents_with_several_entries_and_no_match_needs_an_entry(hass):
    api = await _setup(hass, [API_CODE])
    informed = await _setup_id(hass)

    with pytest.raises(ServiceValidationError) as error:
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
        )

    assert error.value.translation_key == "update_contents_entry_required"
    assert _store(api).get(UNTRACKED) is None
    assert _store(informed).get(UNTRACKED) is None


async def test_entry_id_restricts_and_stores_an_unknown_code(hass):
    api = await _setup(hass, [API_CODE])
    informed = await _setup_id(hass)

    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: UNTRACKED,
            "contents": "Running shoes",
            "entry_id": informed.entry_id,
        },
    )
    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: API_CODE,
            "contents": "Winter socks",
            "entry_id": api.entry_id,
        },
    )

    assert _store(informed).get(UNTRACKED) == "Running shoes"
    assert _store(api).get(UNTRACKED) is None
    assert _store(api).get(API_CODE) == "Winter socks"
    assert _store(informed).get(API_CODE) is None


async def test_entry_id_must_name_a_loaded_entry(hass):
    await _setup(hass, [API_CODE])
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: API_CODE, "entry_id": "missing"},
        )


# ---------------------------------------------------------------------------
# service registration
# ---------------------------------------------------------------------------


async def test_informed_delivery_alone_registers_only_update_contents(hass):
    informed = await _setup_id(hass)

    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")
    assert not hass.services.has_service(DOMAIN, "untrack_parcel")

    assert await hass.config_entries.async_unload(informed.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_unloading_one_entry_keeps_the_services_the_others_need(hass):
    api = await _setup(hass, [API_CODE])
    informed = await _setup_id(hass)
    assert hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(api.entry_id)
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(informed.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_unloading_informed_delivery_keeps_the_hub_services(hass):
    await _setup(hass, [API_CODE])
    informed = await _setup_id(hass)

    assert await hass.config_entries.async_unload(informed.entry_id)

    assert hass.services.has_service(DOMAIN, "track_parcel")
    assert hass.services.has_service(DOMAIN, "update_contents")


async def test_an_api_entry_after_informed_delivery_registers_the_hub_services(hass):
    await _setup_id(hass)
    await _setup(hass, [API_CODE])

    assert hass.services.has_service(DOMAIN, "track_parcel")
    assert hass.services.has_service(DOMAIN, "untrack_parcel")
