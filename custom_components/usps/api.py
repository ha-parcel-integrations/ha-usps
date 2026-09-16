"""Compatibility imports for the API Tracking source."""
from .api_tracking.client import (
    ApiTrackingClient,
    TrackingNotEnabledError,
    USPSApiError,
    USPSAuthError,
)

USPSApiClient = ApiTrackingClient

__all__ = ["ApiTrackingClient", "TrackingNotEnabledError", "USPSApiClient", "USPSApiError", "USPSAuthError"]
