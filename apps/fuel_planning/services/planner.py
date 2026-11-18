"""Fetch the start/end route once, then perform all fuel planning locally."""

import time
from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction

from django.conf import settings

from apps.fuel_planning.types import (
    FuelPlan,
    RouteMeasure,
    RoutePosition,
    RouteSource,
    SearchBudget,
    StationVisit,
    VehicleSpec,
)
from apps.routes.services.directions import (
    directions,
    normalize_options,
    normalize_position,
    validate_profile,
)

from .cache import (
    get_complete_plan,
    get_route_context,
    get_station_attachments,
    load_configured_catalog,
    plan_cache_key,
    store_complete_plan,
)
from .distances import RouteExcursionDistanceProvider
from .numeric import meters_to_miles, validate_fuel
from .optimizer import plan_fuel_stops
from .route_geometry import geodesic_distance_meters


@dataclass(frozen=True, slots=True)
class TripPlan:
    route: RouteMeasure
    fuel_plan: FuelPlan
    catalog_fingerprint: str
    eligible_station_count: int
    excluded_station_count: int
    cache_hit: bool = False
    station_context_count: int = 0


class CurrentStationError(ValueError):
    """An explicitly identified starting pump does not match this trip."""

    field = "current_station_id"

    def __init__(self, message, *, reason):
        super().__init__(message)
        self.reason = reason


def validate_current_station(current_station_id, catalog, start=None):
    """Match explicit identity first, then coordinates within one meter.

    This tolerance reconciles geocoder precision only; it does not establish a
    station entrance or alter any fuel/range comparison.
    """
    if current_station_id is None:
        return None
    station = next(
        (
            station
            for station in catalog.stations
            if station.station_id == current_station_id
        ),
        None,
    )
    if station is None:
        raise CurrentStationError(
            "Current station is not present in the station catalog.",
            reason="unknown_station",
        )
    if not station.eligible:
        raise CurrentStationError(
            "Current station has no usable coordinates or price.",
            reason="ineligible_station",
        )
    point = (station.longitude, station.latitude)
    if start is not None and geodesic_distance_meters(start, point) > 1:
        raise CurrentStationError(
            "Resolved start must match the current station coordinate within one meter.",
            reason="station_location_mismatch",
        )
    return station


def _initial_source(route, visits, current_station):
    # A new trip starts at the first occurrence even on a looping route.
    position = RoutePosition(
        route.fingerprint, 0, Fraction(), Fraction(), route.coordinates[0]
    )
    if current_station is None:
        return RouteSource(position), visits
    point = (current_station.longitude, current_station.latitude)
    connector = meters_to_miles(geodesic_distance_meters(position.coordinates, point))
    initial_visit = StationVisit(
        station_id=current_station.station_id,
        position=position,
        coordinates=point,
        price_per_gallon=current_station.price_per_gallon,
        outbound_miles=connector,
        return_miles=connector,
        coordinate_quality=current_station.coordinate_quality,
        source_csv_record=current_station.source_csv_record,
    )
    # Retain later route occurrences. Replace only the exact same initial context,
    # or add it if the nearest attachment lies farther along the route.
    visits = tuple(
        visit for visit in visits if visit.visit_id != initial_visit.visit_id
    )
    visits = tuple(
        sorted(
            (*visits, initial_visit),
            key=lambda visit: (visit.position.progress_miles, visit.visit_id),
        )
    )
    return RouteSource.at_station(initial_visit), visits


def plan_trip(
    start,
    destination,
    *,
    initial_fuel_gallons=Decimal(50),
    vehicle=None,
    profile="driving-car",
    options=None,
    budget=None,
    directions_client=None,
    catalog=None,
    current_station_id=None,
) -> TripPlan:
    """Plan from resolved [longitude, latitude] endpoints with one route lookup."""
    vehicle = VehicleSpec() if vehicle is None else vehicle
    if not isinstance(vehicle, VehicleSpec):
        raise TypeError("Vehicle must be a VehicleSpec.")
    fuel = validate_fuel(initial_fuel_gallons, vehicle.tank_capacity_gallons)
    start = normalize_position(start)
    destination = normalize_position(destination)
    profile = validate_profile(profile)
    options = normalize_options({} if options is None else options)
    if budget is not None and not isinstance(budget, SearchBudget):
        raise TypeError("budget must be a SearchBudget.")
    # Verify local data before spending the sole routing request.
    catalog = load_configured_catalog() if catalog is None else catalog
    current_station = validate_current_station(current_station_id, catalog, start)
    client = directions if directions_client is None else directions_client
    geojson = client.route(start, destination, profile=profile, options=options)

    # No network-capable service is used beyond this point.
    route = get_route_context(geojson)
    key = plan_cache_key(
        route=route,
        catalog=catalog,
        start=start,
        destination=destination,
        initial_fuel_gallons=fuel,
        vehicle=vehicle,
        profile=profile,
        options=options,
        current_station_id=current_station_id,
    )
    cached = get_complete_plan(key)
    if cached is not None:
        return replace(cached, cache_hit=True)
    visits = get_station_attachments(catalog, route)
    source, visits = _initial_source(route, visits, current_station)
    provider = RouteExcursionDistanceProvider(route, visits, vehicle)
    if budget is None:
        budget = SearchBudget(
            deadline=time.monotonic() + settings.FUEL_SEARCH_TIMEOUT_SECONDS,
            maximum_generated_labels=settings.FUEL_SEARCH_MAX_LABELS,
        )
    fuel_plan = plan_fuel_stops(
        source,
        provider.destination,
        visits,
        vehicle,
        provider,
        initial_fuel_gallons=fuel,
        budget=budget,
    )
    result = TripPlan(
        route=route,
        fuel_plan=fuel_plan,
        catalog_fingerprint=catalog.fingerprint,
        eligible_station_count=catalog.eligible_count,
        excluded_station_count=catalog.excluded_count,
        station_context_count=len(visits),
    )
    store_complete_plan(key, result)
    return result
