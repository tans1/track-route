"""Build the detailed display data used by the trip map."""

import math
from dataclasses import asdict
from fractions import Fraction

from apps.stations.services.catalog import CATALOG_VERSION

from .numeric import as_exact_amount, format_money


def _exact(value):
    amount = as_exact_amount(value)
    return f"{amount.numerator}/{amount.denominator}"


def _quantities(values, *, miles=2):
    """Display fields plus lossless rational values under matching exact keys."""
    return {
        **{
            key: format_money(value, miles if key.endswith("miles") else 6)
            for key, value in values.items()
        },
        "exact": {key: _exact(value) for key, value in values.items()},
    }


def _station_details(station, source_record):
    row = (
        next((row for row in station.rows if row.csv_record == source_record), None)
        if station is not None
        else None
    )
    fields = dict(row.source_values) if row is not None else {}
    return {
        "name": fields.get("Truckstop Name", ""),
        "address": fields.get("Address", ""),
        "city": fields.get("City", ""),
        "state": fields.get("State", ""),
    }


def build_map_snapshot(trip, *, catalog) -> dict:
    """Build the detailed internal snapshot used by the map and invariant tests."""
    plan = trip.fuel_plan
    route = trip.route.geojson
    duration_minutes = math.ceil(
        route["features"][0]["properties"]["summary"]["duration"] / 60
    )
    hours, minutes = divmod(duration_minutes, 60)
    stations = {station.station_id: station for station in catalog.stations}
    stops, access_features = [], []
    for order, stop in enumerate(plan.stops, 1):
        station = stations.get(stop.station_id)
        visit = stop.visit
        quality = (
            visit.coordinate_quality
            if visit is not None
            else station.coordinate_quality
            if station is not None
            else "unknown"
        )
        values = _quantities(
            {
                "incoming_distance_miles": stop.incoming_distance_miles,
                "arrival_fuel_gallons": stop.arrival_fuel_gallons,
                "gallons_purchased": stop.gallons_purchased,
                "departure_fuel_gallons": stop.departure_fuel_gallons,
            }
        )
        values["exact"].update(
            price_per_gallon=_exact(stop.price_per_gallon),
            purchase_cost=_exact(stop.purchase_cost),
        )
        stops.append(
            {
                "order": order,
                "station_id": stop.station_id,
                **_station_details(station, stop.source_csv_record),
                "source_csv_record": stop.source_csv_record,
                "coordinates": list(stop.coordinates),
                "price_per_gallon": str(stop.price_per_gallon),
                "purchase_cost": format_money(stop.purchase_cost),
                "coordinate_quality": quality,
                "visit_id": list(visit.visit_id) if visit is not None else None,
                **values,
            }
        )
        if visit is not None:
            access_features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            list(visit.position.coordinates),
                            list(visit.coordinates),
                        ],
                    },
                    "properties": {
                        "station_id": stop.station_id,
                        "stop_order": order,
                        "outbound_miles": format_money(visit.outbound_miles),
                        "return_miles": format_money(visit.return_miles),
                        "distance_quality": visit.distance_quality,
                        "coordinate_quality": quality,
                        "station_access": plan.quality.station_access,
                    },
                }
            )
    totals = _quantities(
        {
            "baseline_route_miles": trip.route.total_miles,
            "modeled_distance_miles": plan.total_driving_miles,
            "gallons_consumed": plan.gallons_consumed,
            "gallons_purchased": plan.gallons_purchased,
            "initial_fuel_gallons": plan.initial_fuel_gallons,
            "final_fuel_gallons": plan.final_fuel_gallons,
        }
    )
    total_cost = format_money(plan.purchase_cost)
    rounded_stops = sum((Fraction(stop["purchase_cost"]) for stop in stops), Fraction())
    totals.update(
        {
            "fuel_purchase_cost": total_cost,
            "currency": "USD",
            "stop_cost_rounding_adjustment": format_money(
                Fraction(total_cost) - rounded_stops
            ),
        }
    )
    totals["exact"]["fuel_purchase_cost"] = _exact(plan.purchase_cost)
    next_action = None
    if plan.next_action is not None:
        action = plan.next_action
        next_action = {
            "station_id": action.station_id,
            "coordinates": list(action.coordinates),
            "gallons_to_buy": format_money(action.gallons_to_buy, 6),
            "visit_id": list(action.visit_id) if action.visit_id is not None else None,
            "exact": {"gallons_to_buy": _exact(action.gallons_to_buy)},
        }
    return {
        "route": route,
        "route_duration": f"{hours} h {minutes} min" if hours else f"{minutes} min",
        "stops": stops,
        "totals": totals,
        "legs": [
            {
                "origin_id": leg.origin_id,
                "destination_id": leg.destination_id,
                "distance_miles": format_money(leg.miles),
                "distance_quality": leg.distance_quality,
                "exact": {"distance_miles": _exact(leg.miles)},
            }
            for leg in plan.legs
        ],
        "next_action": next_action,
        "quality": asdict(plan.quality),
        "estimated_access": {"type": "FeatureCollection", "features": access_features},
        "vehicle": {
            "mpg": format_money(plan.vehicle.mpg, 0),
            "tank_capacity_gallons": format_money(
                plan.vehicle.tank_capacity_gallons, 0
            ),
            "maximum_range_miles": format_money(plan.vehicle.maximum_range_miles, 0),
        },
        "units": {
            "distance": "miles",
            "fuel": "US gallons",
            "price": "USD per US gallon",
        },
        "assumptions": [
            catalog.price_assumption,
            "Starting fuel is already paid for; total cost includes only fuel purchased during this trip.",
            "Station access is assumed. Straight-line outward/return distances estimate detours and consume fuel.",
            "City fallback coordinates represent a place, not a verified station entrance.",
            "Optimality applies to the forward-route-plus-excursions model only when search_complete is true.",
        ],
        "catalog": {
            "version": CATALOG_VERSION,
            "fingerprint": trip.catalog_fingerprint,
            "price_policy": str(catalog.price_policy),
            "source_row_count": catalog.row_count,
            "eligible_station_count": trip.eligible_station_count,
            "excluded_station_count": trip.excluded_station_count,
            "station_context_count": getattr(trip, "station_context_count", None),
        },
        "statistics": asdict(plan.statistics),
    }
