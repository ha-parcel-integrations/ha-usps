"""Sample USPS API payloads shared by the test modules.

TODO(carrier): replace these with real (redacted) responses from the carrier.
Keep them in one module rather than inline in each test — when the payload
shape turns out to be different from what you assumed, there is then exactly
one place to fix.
"""
from __future__ import annotations

ACTIVE_CODE = "EXAMPLE999999"
DELIVERED_CODE = "EXAMPLE123456"


def event(status_code: str, timestamp, description: str) -> dict:
    """One entry of the carrier's own event timeline."""
    return {
        "statusCode": status_code,
        "timestamp": timestamp,
        "description": description,
    }


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A representative tracking response for a delivered parcel."""
    return {
        "trackingNumber": code,
        "statusCode": "DELIVERED",
        "statusText": "Delivered to the recipient",
        "sender": "Example Shop",
        "recipient": "Jane Doe",
        "deliveredAt": "2026-04-29T13:12:42Z",
        "estimatedDelivery": {"from": None, "to": None},
        "pickupPoint": None,
        "weightKg": 1.25,
        "dimensionsCm": {"length": 30, "width": 20, "height": 10},
        "events": [
            event("DELIVERED", "2026-04-29T13:12:42Z", "Delivered to the recipient"),
            event("OUT_FOR_DELIVERY", "2026-04-29T08:46:00Z", "Out for delivery"),
            event("IN_TRANSIT", "2026-04-28T15:52:17Z", "At the sorting facility"),
            event("REGISTERED", "2026-04-27T23:03:58Z", "Shipment announced"),
        ],
    }


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel with an ETA window."""
    sample = delivered_sample(code)
    sample.update(
        {
            "statusCode": "OUT_FOR_DELIVERY",
            "statusText": "Out for delivery",
            "deliveredAt": None,
            "estimatedDelivery": {
                "from": "2026-04-29T13:00:00Z",
                "to": "2026-04-29T15:00:00Z",
            },
            "events": sample["events"][1:],
        }
    )
    return sample


def pickup_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel waiting at a pickup point."""
    sample = active_sample(code)
    sample.update(
        {
            "statusCode": "AT_PICKUP_POINT",
            "statusText": "Ready for collection",
            "pickupPoint": {"name": "Example Point Central Station"},
        }
    )
    return sample
