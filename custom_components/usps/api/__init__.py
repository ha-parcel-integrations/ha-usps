"""Credentialed API source and backwards-compatible client exports."""

from .client import (
    ApiTrackingClient,
    TrackingNotEnabledError,
    USPSApiError,
    USPSAuthError,
)

USPSApiClient = ApiTrackingClient

__all__ = [
    "ApiTrackingClient",
    "TrackingNotEnabledError",
    "USPSApiClient",
    "USPSApiError",
    "USPSAuthError",
]
