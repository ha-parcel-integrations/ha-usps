"""Tests for the private, IPv4-only Informed Delivery session."""
import socket

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.usps.account.session import (
    async_new_informed_delivery_session,
)


async def test_new_session_is_ipv4_only_and_not_hass_shared(hass):
    session = async_new_informed_delivery_session(hass)
    try:
        assert session.connector.family == socket.AF_INET
        assert session is not async_get_clientsession(hass)
        assert session.connector is not async_get_clientsession(hass).connector
    finally:
        await session.close()
