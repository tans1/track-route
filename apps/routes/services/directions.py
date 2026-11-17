"""ORS driving GeoJSON, preserved in full and cached by the exact request."""

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
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import caches

from apps.locations.services.geocoding import retry_delay

logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 10 * 1024 * 1024


class RoutingError(Exception):
    def __init__(
        self, message, *, reason, status=503, retry_after=None, provider_status=None
    ):
        super().__init__(message)
        self.reason = reason
        self.status = status
        self.retry_after = retry_after
        self.provider_status = provider_status


def normalize_position(position):
    """Normalize numbers without rounding, keeping [longitude, latitude] order."""
    if not isinstance(position, (list, tuple)) or len(position) != 2:
        raise ValueError("Coordinates must be [longitude, latitude].")
    normalized = []
    for value, bound in zip(position, (180, 90), strict=True):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("Coordinates must be finite numbers.")
        number = float(value)
        if not math.isfinite(number) or not -bound <= number <= bound:
            raise ValueError("Coordinates are out of range.")
        normalized.append(number if number != 0 else 0.0)
    return normalized


def normalize_options(options):
    allowed = {"avoid_features", "avoid_borders", "avoid_countries", "avoid_polygons"}
    if not isinstance(options, dict) or set(options) - allowed:
        raise ValueError(
            "Options may contain avoid_features, avoid_borders, avoid_countries, or avoid_polygons."
        )
    if "avoid_features" in options:
        features = options["avoid_features"]
        if not isinstance(features, list) or any(
            not isinstance(item, str)
            or item not in ("highways", "tollways", "ferries", "fords")
            for item in features
        ):
            raise ValueError(
                "avoid_features must be a list of highways, tollways, ferries, or fords."
            )
    if "avoid_borders" in options and options["avoid_borders"] not in (
        "all",
        "controlled",
        "none",
    ):
        raise ValueError("avoid_borders must be all, controlled, or none.")
    if "avoid_countries" in options:
        countries = options["avoid_countries"]
        if not isinstance(countries, list) or any(
            type(value) is not int or value <= 0 for value in countries
        ):
            raise ValueError(
                "avoid_countries must be a list of positive ORS country IDs."
            )
    if "avoid_polygons" in options:
        polygon = options["avoid_polygons"]
        if (
            not isinstance(polygon, dict)
            or polygon.get("type") not in ("Polygon", "MultiPolygon")
            or not isinstance(polygon.get("coordinates"), list)
            or not polygon["coordinates"]
        ):
            raise ValueError(
                "avoid_polygons must be a GeoJSON Polygon or MultiPolygon."
            )
    try:
        # Copy caller data and reject non-JSON/non-finite options before using the provider.
        return json.loads(json.dumps(options, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError(
            "Route options must contain valid, finite JSON values."
        ) from exc


def validate_profile(profile):
    if profile != "driving-car":
        raise ValueError("This endpoint supports the driving-car profile.")
    return profile


def route_cache_key(endpoint, profile, body):
    canonical = json.dumps(
        {"endpoint": endpoint, "profile": profile, "request": body},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "route:v1:" + hashlib.sha256(canonical.encode()).hexdigest()


def validate_geojson(payload):
    """Validate the path and summary without dropping any ORS response fields."""
    try:
        if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
            raise ValueError
        features = payload["features"]
        if not isinstance(features, list) or not features:
            raise ValueError
        for feature in features:
            if (
                feature["type"] != "Feature"
                or feature["geometry"]["type"] != "LineString"
            ):
                raise ValueError
            coordinates = feature["geometry"]["coordinates"]
            if not isinstance(coordinates, list) or len(coordinates) < 2:
                raise ValueError
            for point in coordinates:
                if not isinstance(point, list) or len(point) not in (2, 3):
                    raise ValueError
                normalize_position(point[:2])
                if len(point) == 3 and (
                    type(point[2]) not in (int, float) or not math.isfinite(point[2])
                ):
                    raise ValueError
            properties = feature["properties"]
            for name in ("distance", "duration"):
                value = properties["summary"][name]
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError
            way_points = properties["way_points"]
            if (
                not isinstance(way_points, list)
                or len(way_points) != 2
                or any(type(index) is not int for index in way_points)
                or way_points != [0, len(coordinates) - 1]
            ):
                raise ValueError
        if not isinstance(payload.get("metadata"), dict):
            raise TypeError
        json.dumps(payload, allow_nan=False)
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise RoutingError(
            "ORS returned invalid route GeoJSON.",
            reason="provider_invalid_response",
            status=502,
        ) from exc
    return payload


class DirectionsClient:
    def __init__(self, *, clock=None):
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._cooldown_until = 0.0

    def require_api_key(self):
        key = settings.ORS_API_KEY.strip()
        if not key or any(char.isspace() for char in key):
            raise RoutingError(
                "Set ORS_API_KEY on the server to enable routing.",
                reason="provider_not_configured",
            )
        return key

    def route(self, start, destination, *, profile="driving-car", options=None):
        profile = validate_profile(profile)
        body = {
            "coordinates": [normalize_position(start), normalize_position(destination)],
            "options": normalize_options({} if options is None else options),
            "preference": "recommended",
            "units": "m",
            "instructions": True,
            "geometry_simplify": False,
        }
        api_key = self.require_api_key()
        endpoint = f"{settings.ORS_DIRECTIONS_URL.rstrip('/')}/{profile}/geojson"
        key = route_cache_key(endpoint, profile, body)
        cache = caches["routes"]
        route = cache.get(key)
        if route is not None:
            return route
        with self._lock:
            route = cache.get(key)
            if route is not None:
                return route
            cooldown = self._cooldown_until - self._clock()
            if cooldown > 0:
                raise RoutingError(
                    "Waiting before retrying ORS.",
                    reason="provider_cooldown",
                    retry_after=math.ceil(cooldown),
                )
            try:
                route = self._request(endpoint, api_key, body)
            except RoutingError as exc:
                logger.warning(
                    "Routing failed: reason=%s provider_status=%s",
                    exc.reason,
                    exc.provider_status,
                )
                if exc.retry_after is not None:
                    self._cooldown_until = self._clock() + exc.retry_after
                raise
            cache.set(key, route, timeout=settings.ROUTE_CACHE_TTL)
            return route

    def _request(self, endpoint, api_key, body):
        request = Request(
            endpoint,
            data=json.dumps(body, allow_nan=False).encode(),
            method="POST",
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
                "Accept": "application/geo+json, application/json",
                "User-Agent": "TrackRoute/0.1 (driving directions)",
            },
        )
        try:
            with urlopen(request, timeout=settings.ORS_TIMEOUT) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise RoutingError(
                        "ORS response exceeded the supported size.",
                        reason="provider_response_too_large",
                        status=502,
                    )
                return validate_geojson(json.loads(raw))
        except HTTPError as exc:
            error_code = None
            try:
                error = json.loads(exc.read(65536)).get("error", {})
                error_code = error.get("code") if isinstance(error, dict) else None
            except ValueError, AttributeError, OSError:
                pass
            if error_code in (2009, 2010):
                raise RoutingError(
                    "No drivable route was found between these locations.",
                    reason="route_not_found",
                    status=422,
                    provider_status=exc.code,
                ) from exc
            if exc.code in (401, 403):
                raise RoutingError(
                    "ORS rejected the server API key or its permissions.",
                    reason="provider_auth_error",
                    provider_status=exc.code,
                ) from exc
            if exc.code in (400, 413, 422):
                raise RoutingError(
                    "ORS could not route these locations with the supplied options.",
                    reason="route_request_rejected",
                    status=422,
                    provider_status=exc.code,
                ) from exc
            delay = retry_delay(
                exc.headers.get("Retry-After"), 60 if exc.code == 429 else 5
            )
            reason = (
                "provider_rate_limited" if exc.code == 429 else "provider_http_error"
            )
            raise RoutingError(
                f"ORS returned HTTP {exc.code}.",
                reason=reason,
                retry_after=delay,
                provider_status=exc.code,
            ) from exc
        except (URLError, TimeoutError, OSError, HTTPException) as exc:
            cause = exc.reason if isinstance(exc, URLError) else exc
            if isinstance(cause, TimeoutError):
                reason, message = (
                    "provider_timeout",
                    "ORS timed out. Try again shortly.",
                )
            elif isinstance(cause, ssl.SSLCertVerificationError):
                reason, message = (
                    "provider_tls_error",
                    "Could not verify the ORS HTTPS certificate.",
                )
            elif isinstance(cause, socket.gaierror):
                reason, message = (
                    "provider_dns_error",
                    "Could not resolve the ORS hostname.",
                )
            else:
                reason, message = (
                    "provider_connection_error",
                    "Could not connect to ORS.",
                )
            raise RoutingError(message, reason=reason, retry_after=5) from exc
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise RoutingError(
                "ORS returned invalid JSON.",
                reason="provider_invalid_response",
                status=502,
            ) from exc


directions = DirectionsClient()
