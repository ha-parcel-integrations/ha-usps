"""Privacy regression tests shared by both USPS sources."""
from homeassistant.components.diagnostics import async_redact_data

from custom_components.usps.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)


def test_diagnostics_redact_both_source_credentials_and_address_data():
    data = {
        "consumer_key": "key", "consumer_secret": "secret",
        "refresh_token": "refresh", "access_token": "access",
        "ZIP11": "12345678901", "trackingNumber": "9400000000000000000000",
        "shipperName": "Private sender", "nested": {"deliveryPoint": "01"},
    }
    redacted = async_redact_data(data, TO_REDACT)
    assert all(value == "**REDACTED**" for value in [redacted["consumer_key"], redacted["consumer_secret"], redacted["refresh_token"], redacted["ZIP11"], redacted["trackingNumber"], redacted["nested"]["deliveryPoint"]])


async def test_entry_diagnostics_redact_entry_data(hass):
    from types import SimpleNamespace

    coordinator = SimpleNamespace(data=[], delivered=[], delivered_codes=set(), current_tier_minutes=10, update_interval=None)
    entry = SimpleNamespace(data={"consumer_secret": "secret", "refresh_token": "token"}, options={}, runtime_data=SimpleNamespace(coordinator=coordinator))
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["entry_data"]["consumer_secret"] == "**REDACTED**"
    assert diagnostics["entry_data"]["refresh_token"] == "**REDACTED**"
