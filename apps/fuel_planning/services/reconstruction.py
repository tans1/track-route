"""Reconstruct immutable search history and verify every fuel balance exactly."""

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

from apps.fuel_planning.types import (
    FuelPlan,
    FuelStop,
    NextAction,
    PlanQuality,
    RoutePosition,
    SearchStatistics,
    StationVisit,
    VehicleSpec,
    VisitKey,
)

from .labels import SearchLabel
from .numeric import (
    as_exact_amount,
    gallons_for_distance,
    nonnegative_amount,
    validate_fuel,
)


class FuelConservationError(ValueError):
    """A search history or returned itinerary has inconsistent fuel accounting."""


def _amount(value):
    try:
        return nonnegative_amount(value)
    except (TypeError, ValueError) as exc:
        raise FuelConservationError(
            "Fuel accounting needs finite nonnegative amounts."
        ) from exc


def _fuel(value, capacity):
    try:
        return validate_fuel(value, capacity)
    except (TypeError, ValueError) as exc:
        raise FuelConservationError(
            "Fuel is outside the vehicle's tank capacity."
        ) from exc


def _price(value):
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise FuelConservationError("Fuel price must be a finite nonnegative Decimal.")
    return as_exact_amount(value)


def _history(destination_label, labels):
    if destination_label.visit_id is not None:
        raise FuelConservationError(
            "Reconstruction must finish at a destination label."
        )
    history = []
    seen = set()
    current = destination_label
    while True:
        if current.label_id in seen:
            raise FuelConservationError("Search predecessor history contains a cycle.")
        seen.add(current.label_id)
        if labels.get(current.label_id) != current:
            raise FuelConservationError("Search history is missing an immutable label.")
        history.append(current)
        if current.predecessor_id is None:
            break
        try:
            current = labels[current.predecessor_id]
        except KeyError as exc:
            raise FuelConservationError(
                "Search history is missing a predecessor."
            ) from exc
        if current.visit_id is None:
            raise FuelConservationError("A destination cannot precede another leg.")
    return tuple(reversed(history))


def reconstruct_plan(
    destination_label: SearchLabel,
    labels: Mapping[int, SearchLabel],
    visits: Mapping[VisitKey, StationVisit],
    *,
    initial_fuel_gallons,
    vehicle: VehicleSpec,
    destination: RoutePosition,
    quality: PlanQuality,
    statistics: SearchStatistics,
) -> FuelPlan:
    """Keep all driven legs, but list only stops with a positive purchase."""
    history = _history(destination_label, labels)
    capacity = vehicle.tank_capacity_gallons
    initial_fuel = _fuel(initial_fuel_gallons, capacity)
    fuel = initial_fuel
    cost = Fraction()
    distance_since_purchase = Fraction()
    stops = []
    legs = []
    previous = None
    for label in history:
        if label.visit_id is not None and label.visit_id not in visits:
            raise FuelConservationError(
                "Search history references an unknown station visit."
            )
        action = label.action
        purchase = _amount(action.gallons_purchased)
        price = _price(action.price_per_gallon)
        if previous is None:
            if purchase:
                raise FuelConservationError(
                    "A source seed cannot buy fuel without a pump visit."
                )
        elif previous.action.leg.destination_id != action.leg.origin_id:
            raise FuelConservationError("Search history has disconnected travel legs.")
        if purchase:
            visit = visits[previous.visit_id]
            if action.price_per_gallon != visit.price_per_gallon:
                raise FuelConservationError(
                    "Purchase price differs from its station quote."
                )
            departure = _fuel(fuel + purchase, capacity)
            stops.append(
                FuelStop(
                    station_id=visit.station_id,
                    coordinates=visit.coordinates,
                    price_per_gallon=visit.price_per_gallon,
                    arrival_fuel_gallons=fuel,
                    gallons_purchased=purchase,
                    departure_fuel_gallons=departure,
                    incoming_distance_miles=distance_since_purchase,
                    source_csv_record=visit.source_csv_record,
                    visit=visit,
                )
            )
            fuel = departure
            distance_since_purchase = Fraction()
        miles = _amount(action.leg.miles)
        fuel = _fuel(fuel - gallons_for_distance(miles, vehicle.mpg), capacity)
        cost += purchase * price
        if _fuel(label.arrival_fuel, capacity) != fuel:
            raise FuelConservationError(
                "Search label arrival fuel does not match its action."
            )
        if _amount(label.cost) != cost:
            raise FuelConservationError(
                "Search label cost does not match its purchases."
            )
        distance_since_purchase += miles
        legs.append(action.leg)
        previous = label
    plan = FuelPlan(
        stops=tuple(stops),
        legs=tuple(legs),
        initial_fuel_gallons=initial_fuel,
        final_fuel_gallons=fuel,
        quality=quality,
        vehicle=vehicle,
        statistics=statistics,
    )
    verify_fuel_conservation(plan)
    return replace(plan, next_action=build_next_action(plan, destination))


def verify_fuel_conservation(plan: FuelPlan) -> None:
    """Validate purchase balances, capacity, physical leg boundaries, and totals.

    Stop distances are measured since the preceding purchase. They may include
    multiple physical legs, such as a visited station where no fuel was bought.
    """
    capacity = plan.vehicle.tank_capacity_gallons
    initial = _fuel(plan.initial_fuel_gallons, capacity)
    final = _fuel(plan.final_fuel_gallons, capacity)
    fuel = initial
    miles = Fraction()
    purchase_positions = []
    purchase_miles = Fraction()
    purchased = Fraction()
    for stop in plan.stops:
        purchase_miles += _amount(stop.incoming_distance_miles)
        purchase = _amount(stop.gallons_purchased)
        if not purchase:
            raise FuelConservationError(
                "Purchase stops must buy a positive amount of fuel."
            )
        _price(stop.price_per_gallon)
        arrival = _fuel(stop.arrival_fuel_gallons, capacity)
        departure = _fuel(stop.departure_fuel_gallons, capacity)
        if arrival + purchase != departure:
            raise FuelConservationError(
                "A stop's departure fuel does not match its purchase."
            )
        purchase_positions.append((purchase_miles, stop))
        purchased += purchase
    stop_index = 0
    previous_leg = None
    for leg in plan.legs:
        if previous_leg is not None and previous_leg.destination_id != leg.origin_id:
            raise FuelConservationError("Plan travel legs are disconnected.")
        while stop_index < len(purchase_positions):
            position, stop = purchase_positions[stop_index]
            if position != miles:
                break
            if stop.arrival_fuel_gallons != fuel:
                raise FuelConservationError(
                    "A stop's arrival fuel does not match driven distance."
                )
            fuel = _fuel(fuel + stop.gallons_purchased, capacity)
            stop_index += 1
        length = _amount(leg.miles)
        fuel = _fuel(fuel - gallons_for_distance(length, plan.vehicle.mpg), capacity)
        miles += length
        if (
            stop_index < len(purchase_positions)
            and purchase_positions[stop_index][0] < miles
        ):
            raise FuelConservationError(
                "A purchase must occur at a physical leg boundary."
            )
        previous_leg = leg
    if stop_index != len(purchase_positions):
        raise FuelConservationError("A purchase has no outgoing physical leg.")
    if (
        fuel != final
        or initial + purchased - gallons_for_distance(miles, plan.vehicle.mpg) != final
    ):
        raise FuelConservationError(
            "Plan's final fuel does not match total fuel conservation."
        )


def build_next_action(plan: FuelPlan, destination: RoutePosition) -> NextAction:
    """Identify the first positive purchase, or travel directly to the destination."""
    for stop in plan.stops:
        if stop.gallons_purchased > 0:
            return NextAction(
                station_id=stop.station_id,
                coordinates=stop.coordinates,
                gallons_to_buy=stop.gallons_purchased,
                visit_id=stop.visit.visit_id if stop.visit is not None else None,
            )
    return NextAction(station_id=None, coordinates=destination.coordinates)
