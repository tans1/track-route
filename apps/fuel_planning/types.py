"""Immutable, Django-independent contracts for station movement and fuel planning."""

import hashlib
import json
import math
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction

from .services.numeric import (
    as_exact_amount,
    coordinates,
    gallons_for_distance,
    meters_to_miles,
    nonnegative_amount,
)

Coordinate = tuple[float, float]
VisitKey = tuple[str, str, str]
MOVEMENT_MODEL_VERSION = "route-excursions-v1"


@dataclass(frozen=True, slots=True)
class VehicleSpec:
    mpg: Fraction = Fraction(10)
    tank_capacity_gallons: Fraction = Fraction(50)

    def __post_init__(self):
        for name in ("mpg", "tank_capacity_gallons"):
            amount = as_exact_amount(getattr(self, name))
            if amount <= 0:
                raise ValueError("Vehicle economy and capacity must be positive.")
            object.__setattr__(self, name, amount)

    @property
    def maximum_range_miles(self) -> Fraction:
        return self.mpg * self.tank_capacity_gallons


@dataclass(frozen=True, slots=True)
class RouteMeasure:
    fingerprint: str
    coordinates: tuple[Coordinate, ...]
    segment_lengths_meters: tuple[Fraction, ...]
    cumulative_meters: tuple[Fraction, ...]
    total_distance_meters: Fraction
    distance_scale: Fraction
    projection_center: Coordinate
    original_geojson: str = field(repr=False)
    model_version: str = MOVEMENT_MODEL_VERSION

    @property
    def total_miles(self) -> Fraction:
        return meters_to_miles(self.total_distance_meters)

    @property
    def geojson(self) -> dict:
        # A caller cannot mutate the route used for fingerprints or attachments.
        return json.loads(self.original_geojson)


@dataclass(frozen=True, slots=True)
class RoutePosition:
    route_id: str
    segment_index: int
    fraction: Fraction
    progress_miles: Fraction
    coordinates: Coordinate
    _attachment_id: str = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if (
            not self.route_id
            or type(self.segment_index) is not int
            or self.segment_index < 0
        ):
            raise ValueError(
                "A position needs route identity and a nonnegative segment index."
            )
        fraction = as_exact_amount(self.fraction)
        if not 0 <= fraction <= 1:
            raise ValueError("Segment fraction must be between zero and one.")
        object.__setattr__(self, "fraction", fraction)
        object.__setattr__(
            self, "progress_miles", nonnegative_amount(self.progress_miles)
        )
        object.__setattr__(self, "coordinates", coordinates(self.coordinates))
        identity = f"{self.route_id}:{self.segment_index}:{fraction}"
        object.__setattr__(
            self, "_attachment_id", hashlib.sha256(identity.encode()).hexdigest()
        )

    @property
    def attachment_id(self) -> str:
        return self._attachment_id


@dataclass(frozen=True, slots=True)
class StationVisit:
    station_id: str
    position: RoutePosition
    coordinates: Coordinate
    price_per_gallon: Decimal
    outbound_miles: Fraction
    return_miles: Fraction
    coordinate_quality: str = "unknown"
    distance_quality: str = "estimated_geodesic"
    access_context_id: str = "geodesic-excursion-v1"
    source_csv_record: int | None = None

    def __post_init__(self):
        if not self.station_id or not self.access_context_id:
            raise ValueError(
                "A station visit needs station and access-context identities."
            )
        price = self.price_per_gallon
        if not isinstance(price, Decimal) or not price.is_finite() or price < 0:
            raise ValueError("Station prices must be finite nonnegative Decimals.")
        object.__setattr__(self, "coordinates", coordinates(self.coordinates))
        for name in ("outbound_miles", "return_miles"):
            object.__setattr__(self, name, nonnegative_amount(getattr(self, name)))

    @property
    def visit_id(self) -> VisitKey:
        return self.station_id, self.position.attachment_id, self.access_context_id


@dataclass(frozen=True, slots=True)
class RouteSource:
    position: RoutePosition
    station_visit_id: VisitKey | None = None
    coordinates: Coordinate | None = None

    def __post_init__(self):
        point = (
            self.position.coordinates if self.coordinates is None else self.coordinates
        )
        object.__setattr__(self, "coordinates", coordinates(point))

    @property
    def kind(self) -> str:
        return "at_station" if self.station_visit_id is not None else "on_route"

    @classmethod
    def at_station(cls, visit: StationVisit):
        return cls(visit.position, visit.visit_id, visit.coordinates)


@dataclass(frozen=True, slots=True)
class LegDistance:
    miles: Fraction
    origin_id: str
    destination_id: str
    distance_quality: str
    movement_model: str = MOVEMENT_MODEL_VERSION

    def __post_init__(self):
        object.__setattr__(self, "miles", nonnegative_amount(self.miles))


@dataclass(frozen=True, slots=True)
class FuelAction:
    leg: LegDistance
    gallons_purchased: Fraction
    price_per_gallon: Decimal


@dataclass(frozen=True, slots=True)
class FuelStop:
    station_id: str
    coordinates: Coordinate
    price_per_gallon: Decimal
    arrival_fuel_gallons: Fraction
    gallons_purchased: Fraction
    departure_fuel_gallons: Fraction
    incoming_distance_miles: Fraction
    source_csv_record: int | None = None
    visit: StationVisit | None = None

    @property
    def purchase_cost(self) -> Fraction:
        return self.gallons_purchased * as_exact_amount(self.price_per_gallon)


@dataclass(frozen=True, slots=True)
class PlanQuality:
    movement_model: str = MOVEMENT_MODEL_VERSION
    distance_quality: str = "estimated_access"
    station_location_quality: str = "unverified"
    station_access: str = "assumed_reachable"
    search_complete: bool = False
    optimality_scope: str = "not_established"


@dataclass(frozen=True, slots=True)
class NextAction:
    station_id: str | None
    coordinates: Coordinate
    gallons_to_buy: Fraction = Fraction()
    visit_id: VisitKey | None = None


@dataclass(frozen=True, slots=True)
class SearchStatistics:
    generated_labels: int = 0
    accepted_labels: int = 0
    settled_labels: int = 0
    transitions: int = 0
    stale_pops: int = 0
    elapsed_seconds: float = 0.0
    stop_reason: str = "not_started"


@dataclass(frozen=True, slots=True)
class FuelPlan:
    stops: tuple[FuelStop, ...]
    legs: tuple[LegDistance, ...]
    initial_fuel_gallons: Fraction
    final_fuel_gallons: Fraction
    quality: PlanQuality
    vehicle: VehicleSpec = field(default_factory=VehicleSpec)
    next_action: NextAction | None = None
    statistics: SearchStatistics = field(default_factory=SearchStatistics)

    @property
    def total_driving_miles(self) -> Fraction:
        return sum((leg.miles for leg in self.legs), Fraction())

    @property
    def gallons_consumed(self) -> Fraction:
        return gallons_for_distance(self.total_driving_miles, self.vehicle.mpg)

    @property
    def gallons_purchased(self) -> Fraction:
        return sum((stop.gallons_purchased for stop in self.stops), Fraction())

    @property
    def purchase_cost(self) -> Fraction:
        return sum((stop.purchase_cost for stop in self.stops), Fraction())


@dataclass(frozen=True, slots=True)
class SearchBudget:
    deadline: float | None = None
    maximum_generated_labels: int = 200000

    def __post_init__(self):
        if self.deadline is not None and (
            isinstance(self.deadline, bool)
            or not isinstance(self.deadline, (int, float))
            or not math.isfinite(self.deadline)
        ):
            raise ValueError("Search deadline must be a finite monotonic time.")
        if (
            type(self.maximum_generated_labels) is not int
            or self.maximum_generated_labels < 1
        ):
            raise ValueError("The label budget must be positive.")
