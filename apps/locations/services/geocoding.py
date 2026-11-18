"""US request-location lookup with a bounded memory cache and serial requests.

The public provider requires one application process: the cache, request lock,
and rate limit are process-local. See README before deploying multiple workers.
"""

import hashlib
import json
import logging
import math
import socket
import ssl
import threading
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import caches

from apps.common.metrics import provider_attempt
from apps.common.providers import ProviderError, retry_delay

logger = logging.getLogger(__name__)


class LocationNotFound(Exception):
    pass


def normalize_query(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValueError("Provide a location name or address of 1–300 characters.")
    if any(ord(char) < 32 and not char.isspace() for char in value):
        raise ValueError("Location must not contain control characters.")
    return " ".join(value.split())


def select_us_result(payload):
    if not isinstance(payload, list):
        raise ProviderError(
            "Geocoding provider returned an invalid response.", status=502
        )
    for candidate in payload:
        if not isinstance(candidate, dict):
            raise ProviderError(
                "Geocoding provider returned an invalid response.", status=502
            )
        address = candidate.get("address")
        country = address.get("country_code") if isinstance(address, dict) else None
        if not isinstance(country, str) or country.lower() != "us":
            continue
        try:
            if isinstance(candidate.get("lat"), bool) or isinstance(
                candidate.get("lon"), bool
            ):
                raise TypeError
            latitude, longitude = float(candidate["lat"]), float(candidate["lon"])
            label = candidate["display_name"]
            if (
                not math.isfinite(latitude)
                or not math.isfinite(longitude)
                or not -90 <= latitude <= 90
                or not -180 <= longitude <= 180
                or not isinstance(label, str)
                or not label.strip()
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ProviderError(
                "Geocoding provider returned an invalid location.", status=502
            ) from exc
        return {
            "latitude": latitude,
            "longitude": longitude,
            "display_name": label,
            "country_code": "us",
        }
    return None


class LocationResolver:
    def __init__(self, *, clock=None, sleep=None):
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._next_request = 0.0
        self._cooldown_until = 0.0

    def resolve(self, query):
        query = normalize_query(query)
        endpoint = settings.NOMINATIM_SEARCH_URL
        digest = hashlib.sha256(
            json.dumps([endpoint, query.casefold()]).encode()
        ).hexdigest()
        key = f"location:us:v1:{digest}"
        cache = caches["locations"]
        missing = object()
        result = cache.get(key, missing)
        if result is missing:
            # Recheck under the lock so simultaneous identical searches call once.
            with self._lock:
                result = cache.get(key, missing)
                if result is missing:
                    cooldown = self._cooldown_until - self._clock()
                    if cooldown > 0:
                        raise ProviderError(
                            "Waiting before retrying the geocoding provider.",
                            retry_after=math.ceil(cooldown),
                            reason="provider_cooldown",
                        )
                    delay = self._next_request - self._clock()
                    if delay > 0:
                        self._sleep(delay)
                    try:
                        result = self._search(endpoint, query)
                    except ProviderError as exc:
                        # Do not log submitted addresses, full URLs, or provider response bodies.
                        logger.warning(
                            "Geocoding failed: reason=%s provider_status=%s cause=%s",
                            exc.reason,
                            exc.provider_status,
                            type(exc.__cause__).__name__ if exc.__cause__ else "none",
                        )
                        self._cooldown_until = self._clock() + (exc.retry_after or 5)
                        raise
                    finally:
                        # Space requests from completion, including failed attempts.
                        self._next_request = self._clock() + 1.1
                    timeout = (
                        settings.LOCATION_CACHE_TTL
                        if result is not None
                        else settings.LOCATION_MISS_CACHE_TTL
                    )
                    cache.set(key, result, timeout=timeout)
        if result is None:
            raise LocationNotFound(
                "No matching US location found. Include a city and state or full address."
            )
        return {"query": query, **result}

    def _search(self, endpoint, query):
        params = urlencode(
            {
                "q": query,
                "format": "jsonv2",
                "countrycodes": "us",
                "addressdetails": 1,
                "limit": 3,
                "accept-language": "en",
            }
        )
        request = Request(
            f"{endpoint}?{params}",
            headers={
                "User-Agent": settings.NOMINATIM_USER_AGENT,
                "Accept": "application/json",
            },
        )
        try:
            with (
                provider_attempt("nominatim"),
                urlopen(request, timeout=settings.NOMINATIM_TIMEOUT) as response,
            ):
                body = response.read(1_000_001)
                if len(body) > 1_000_000:
                    raise ProviderError(
                        "Geocoding provider returned an oversized response.", status=502
                    )
                return select_us_result(json.loads(body))
        except HTTPError as exc:
            delay = retry_delay(
                exc.headers.get("Retry-After"), 60 if exc.code == 429 else 5
            )
            reason = (
                "provider_rate_limited" if exc.code == 429 else "provider_http_error"
            )
            message = (
                "Geocoding provider rate-limited the request."
                if exc.code == 429
                else f"Geocoding provider returned HTTP {exc.code}."
            )
            raise ProviderError(
                message, retry_after=delay, reason=reason, provider_status=exc.code
            ) from exc
        except (URLError, TimeoutError, OSError, HTTPException) as exc:
            cause = exc.reason if isinstance(exc, URLError) else exc
            if isinstance(cause, TimeoutError):
                reason, message = (
                    "provider_timeout",
                    "Geocoding provider timed out. Try again shortly.",
                )
            elif isinstance(cause, ssl.SSLCertVerificationError):
                reason = "provider_tls_error"
                message = "Could not verify the geocoding provider's HTTPS certificate."
            elif isinstance(cause, socket.gaierror):
                reason, message = (
                    "provider_dns_error",
                    "Could not resolve the geocoding provider's hostname.",
                )
            else:
                reason, message = (
                    "provider_connection_error",
                    "Could not connect to the geocoding provider.",
                )
            raise ProviderError(message, retry_after=5, reason=reason) from exc
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ProviderError(
                "Geocoding provider returned an invalid response.", status=502
            ) from exc


resolver = LocationResolver()
