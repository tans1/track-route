"""Validate trip API inputs and serialize the public response."""

import math
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from apps.locations.services.geocoding import normalize_query
from apps.routes.services.directions import normalize_options, validate_profile


class FuelPlanRequestError(ValueError):
    def __init__(self, code, message, *, field=None, status=400):
        super().__init__(message)
        self.code = code
        self.field = field
        self.status = status


@dataclass(frozen=True, slots=True)
class FuelPlanRequest:
    start: str
    destination: str
    initial_fuel_gallons: Fraction
    profile: str
    options: dict
    current_station_id: str | None = None


def _initial_fuel(value):
    # Bound input precision before constructing a potentially enormous Fraction.
    # HTTP JSON decimals arrive as Decimal; no binary float conversion occurs.
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise TypeError("Initial fuel must be a decimal number between 0 and 50.")
    if isinstance(value, str) and len(value) > 256:
        raise ValueError("Initial fuel has too many digits.")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(
            "Initial fuel must be a decimal number between 0 and 50."
        ) from exc
    if not amount.is_finite() or not 0 <= amount <= 50:
        raise ValueError("Initial fuel must be between 0 and 50 gallons.")
    digits = amount.as_tuple()
    if len(digits.digits) > 128 or abs(digits.exponent) > 128:
        raise ValueError("Initial fuel supports at most 128 decimal digits and places.")
    return Fraction(amount)


def _option_numbers(value):
    """ORS geometry uses JSON numbers; fuel is converted independently above."""
    if isinstance(value, Decimal):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Route options must contain finite numbers.")
        return number
    if isinstance(value, list):
        return [_option_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: _option_numbers(item) for key, item in value.items()}
    return value


def parse_fuel_plan_request(payload) -> FuelPlanRequest:
    required = {"start", "destination"}
    optional = {"initial_fuel_gallons", "profile", "options", "current_station_id"}
    if (
        not isinstance(payload, dict)
        or not required.issubset(payload)
        or set(payload) - required - optional
    ):
        raise FuelPlanRequestError(
            "invalid_fields",
            "Provide start and destination, with optional initial_fuel_gallons, "
            "profile, options, and current_station_id.",
        )
    locations = {}
    for field in ("start", "destination"):
        try:
            locations[field] = normalize_query(payload[field])
        except ValueError as exc:
            raise FuelPlanRequestError(
                "invalid_location", str(exc), field=field
            ) from exc
    try:
        fuel = _initial_fuel(payload.get("initial_fuel_gallons", 50))
    except (ValueError, TypeError, OverflowError) as exc:
        raise FuelPlanRequestError(
            "invalid_initial_fuel", str(exc), field="initial_fuel_gallons"
        ) from exc
    try:
        profile = validate_profile(payload.get("profile", "driving-car"))
        options = normalize_options(_option_numbers(payload.get("options", {})))
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise FuelPlanRequestError("invalid_route_options", str(exc)) from exc
    station_id = payload.get("current_station_id")
    if station_id is not None:
        if (
            not isinstance(station_id, str)
            or not station_id.strip()
            or len(station_id) > 128
            or any(ord(character) < 32 for character in station_id)
        ):
            raise FuelPlanRequestError(
                "invalid_current_station",
                "current_station_id must be a nonempty station ID of at most 128 characters.",
                field="current_station_id",
            )
        station_id = station_id.strip()
    return FuelPlanRequest(
        **locations,
        initial_fuel_gallons=fuel,
        profile=profile,
        options=options,
        current_station_id=station_id,
    )


def serialize_trip_response(snapshot, *, map_url) -> dict:
    """Expose trip essentials and every path coordinate; keep detail on the map.

    Explicit fields keep internal provenance, exact arithmetic, provider details,
    and search diagnostics out of the public response as the snapshot evolves.
    """
    feature = snapshot["route"]["features"][0]
    totals = snapshot["totals"]
    location_types = {
        "representative_place_internal_point": "city",
        "interpolated_address": "address",
    }
    stops = [
        {
            **{
                field: stop[field]
                for field in (
                    "order",
                    "station_id",
                    "name",
                    "address",
                    "city",
                    "state",
                    "price_per_gallon",
                    "gallons_purchased",
                    "purchase_cost",
                )
            },
            "coordinates": list(stop["coordinates"]),
            "location_type": location_types.get(stop["coordinate_quality"], "unknown"),
        }
        for stop in snapshot["stops"]
    ]
    public_totals = {
        "distance_miles": totals["modeled_distance_miles"],
        **{
            field: totals[field]
            for field in (
                "gallons_purchased",
                "gallons_consumed",
                "fuel_purchase_cost",
                "currency",
            )
        },
    }
    if totals["stop_cost_rounding_adjustment"] != "0.00":
        public_totals["rounding_adjustment"] = totals["stop_cost_rounding_adjustment"]
    return {
        **{
            field: {
                "name": snapshot[field]["display_name"],
                "coordinates": [
                    snapshot[field]["longitude"],
                    snapshot[field]["latitude"],
                ],
            }
            for field in ("start", "destination")
        },
        "path": deepcopy(feature["geometry"]["coordinates"]),
        "stops": stops,
        "totals": public_totals,
        "search_complete": snapshot["quality"]["search_complete"],
        "map_url": map_url,
    }
