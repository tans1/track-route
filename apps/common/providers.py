"""Shared external-provider errors and Retry-After parsing."""

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


class ProviderError(Exception):
    def __init__(
        self,
        message,
        *,
        status=503,
        retry_after=None,
        reason=None,
        provider_status=None,
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.reason = reason or (
            "provider_invalid_response" if status == 502 else "provider_unavailable"
        )
        self.provider_status = provider_status


def retry_delay(value, default):
    """Accept both seconds and HTTP dates in Retry-After."""
    try:
        return max(1, int(value))
    except TypeError, ValueError:
        try:
            date = parsedate_to_datetime(value)
            return max(1, math.ceil((date - datetime.now(UTC)).total_seconds()))
        except TypeError, ValueError, OverflowError:
            return default
