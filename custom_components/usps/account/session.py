"""A private aiohttp session for Informed Delivery.

USPS's sign-in host sits behind an edge that answers the credentials round
with HTTP 503 when the request travels over Home Assistant's pooled
connector — also through ``async_create_clientsession``, which reuses that
same connector. A connector owned by this integration does not hit it.

IPv4 is forced because the host also publishes IPv6 addresses, and on a
network without a working IPv6 route the connection attempt fails
intermittently instead of falling back cleanly.
"""
from __future__ import annotations

import socket

import aiohttp
from homeassistant.core import HomeAssistant


def async_new_informed_delivery_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """Return a new session on its own IPv4-only connector; the caller closes it."""
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            family=socket.AF_INET, resolver=aiohttp.ThreadedResolver(), ttl_dns_cache=300
        )
    )
