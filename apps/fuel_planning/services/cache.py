"""Bounded, process-local snapshots for route models, complete plans, and maps."""

import hashlib
import json
from uuid import UUID, uuid4

from django.conf import settings
from django.core.cache import caches

from apps.stations.services.catalog import (
    StationCatalog,
    catalog_fingerprint,
    load_station_catalog,
)
from apps.stations.services.csv_data import fingerprint

from ..types import MOVEMENT_MODEL_VERSION, RouteMeasure, StationVisit
from .numeric import as_exact_amount
from .route_geometry import (
    GEOMETRY_VERSION,
    ModelGeometryError,
    attach_stations,
    build_route_measure,
)


def load_configured_catalog() -> StationCatalog:
    """Rehash all artifacts before reusing a verified catalog snapshot."""
    raw_path = settings.FUEL_STATIONS_RAW_PATH
    csv_path = settings.FUEL_STATIONS_CSV_PATH
    report_path = settings.FUEL_STATIONS_REPORT_PATH
    policy = settings.FUEL_PRICE_POLICY
    identity = catalog_fingerprint(
        fingerprint(raw_path), fingerprint(csv_path), fingerprint(report_path), policy
    )
    cache = caches["station_catalogs"]
    cached = cache.get(f"station-catalog:{identity}")
    if cached is not None:
        return cached
    catalog = load_station_catalog(csv_path, report_path, policy, raw_path=raw_path)

    cache.set(
        f"station-catalog:{catalog.fingerprint}",
        catalog,
        timeout=settings.FUEL_MODEL_CACHE_TTL,
    )
    return catalog


def get_station_attachments(
    catalog: StationCatalog, measure: RouteMeasure
) -> tuple[StationVisit, ...]:
    """Cache complete attachments by catalog, route and supported model versions."""
    configured_version = settings.FUEL_MOVEMENT_MODEL_VERSION
    if configured_version != MOVEMENT_MODEL_VERSION:
        raise ModelGeometryError(
            "Configured fuel movement model version is unsupported."
        )
    if measure.model_version != MOVEMENT_MODEL_VERSION:
        raise ModelGeometryError(
            "Route measure uses an unsupported movement model version."
        )
    identity = json.dumps(
        [
            catalog.fingerprint,
            measure.fingerprint,
            measure.model_version,
            configured_version,
        ],
        separators=(",", ":"),
    )
    key = "station-attachments:" + hashlib.sha256(identity.encode()).hexdigest()
    cache = caches["station_attachments"]
    cached = cache.get(key)
    if cached is not None:
        return cached
    visits = attach_stations(catalog, measure)
    cache.set(key, visits, timeout=settings.FUEL_MODEL_CACHE_TTL)
    return visits


def _digest(payload):
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def get_route_context(geojson):
    """Reuse a measured route only when its full response and model agree."""
    if settings.FUEL_MOVEMENT_MODEL_VERSION != MOVEMENT_MODEL_VERSION:
        raise ModelGeometryError(
            "Configured fuel movement model version is unsupported."
        )
    key = "route-context:" + _digest(
        [geojson, MOVEMENT_MODEL_VERSION, GEOMETRY_VERSION]
    )
    cache = caches["route_contexts"]
    measure = cache.get(key)
    if measure is None:
        measure = build_route_measure(geojson)
        cache.set(key, measure, timeout=settings.FUEL_MODEL_CACHE_TTL)
    return measure


def plan_cache_key(
    *,
    route,
    catalog,
    start,
    destination,
    initial_fuel_gallons,
    vehicle,
    profile,
    options,
    current_station_id=None,
):
    """Exact model identity; no display rounding or geographic truncation."""
    return "fuel-plan:" + _digest(
        {
            "route": route.fingerprint,
            "catalog": catalog.fingerprint,
            "price_policy": str(catalog.price_policy),
            "start": start,
            "destination": destination,
            "initial_fuel_gallons": str(as_exact_amount(initial_fuel_gallons)),
            "vehicle": [str(vehicle.mpg), str(vehicle.tank_capacity_gallons)],
            "profile": profile,
            "options": options,
            "current_station_id": current_station_id,
            "source_context": "initial-route-occurrence-v1",
            "station_access": "assumed_reachable",
            "pump_tolerance_meters": 1,
            "model": route.model_version,
            "geometry": GEOMETRY_VERSION,
            "solver": settings.FUEL_SOLVER_VERSION,
            "routing_provider": settings.ORS_DIRECTIONS_URL,
        }
    )


def get_complete_plan(key):
    plan = caches["fuel_plans"].get(key)
    if plan is None or not plan.fuel_plan.quality.search_complete:
        return None
    return plan


def store_complete_plan(key, plan):
    if plan.fuel_plan.quality.search_complete:
        caches["fuel_plans"].set(key, plan, timeout=settings.FUEL_PLAN_CACHE_TTL)


def store_map_plan(payload):
    """Store an immutable JSON snapshot, including explicitly incomplete plans."""
    plan_id = str(uuid4())
    encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"))
    caches["fuel_maps"].set(
        f"fuel-map:{plan_id}", encoded, timeout=settings.FUEL_MAP_CACHE_TTL
    )
    return plan_id


def get_map_plan(plan_id):
    try:
        plan_id = str(UUID(str(plan_id)))
    except TypeError, ValueError, AttributeError:
        return None
    encoded = caches["fuel_maps"].get(f"fuel-map:{plan_id}")
    return json.loads(encoded) if encoded is not None else None
