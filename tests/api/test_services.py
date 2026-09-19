"""Tests that global services cannot mutate the inbox source."""
import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.usps.const import (
    CONF_SOURCE,
    DOMAIN,
    SOURCE_API_TRACKING,
    SOURCE_INFORMED_DELIVERY,
)
from custom_components.usps.services import _resolve_entry


def test_service_resolution_rejects_an_informed_delivery_target(hass):
    inbox = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_INFORMED_DELIVERY})
    inbox.add_to_hass(hass)
    with pytest.raises(ServiceValidationError):
        _resolve_entry(hass, inbox.entry_id)


def test_service_resolution_uses_the_only_api_tracking_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_API_TRACKING})
    entry.add_to_hass(hass)
    assert _resolve_entry(hass) is entry


def test_service_requires_entry_id_with_multiple_api_tracking_entries(hass):
    first = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_API_TRACKING})
    second = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_API_TRACKING})
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    with pytest.raises(ServiceValidationError):
        _resolve_entry(hass)
    assert _resolve_entry(hass, second.entry_id) is second
