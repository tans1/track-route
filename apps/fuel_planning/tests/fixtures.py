"""Small synthetic catalogs, routes, and station visits for local tests."""

import hashlib
from decimal import Decimal
from fractions import Fraction

from apps.fuel_planning.services.numeric import METERS_PER_MILE
from apps.fuel_planning.types import (
    RouteMeasure,
    RoutePosition,
    StationVisit,
)
from apps.stations.services.catalog import (
    PricePolicy,
    Station,
    StationCatalog,
)


def route_payload(miles=1200):
    points = [
        [-120.0, 35.0],
        [-116.5, 35.0],
        [-115.5, 35.0],
        [-112.0, 35.0],
        [-111.0, 35.0],
        [-108.0, 35.0],
    ]
    distance = float(Fraction(miles) * METERS_PER_MILE)
    return {
        "type": "FeatureCollection",
        "bbox": [-120.0, 35.0, -108.0, 35.0],
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": points},
                "properties": {
                    "summary": {"distance": distance, "duration": 70000.0},
                    "segments": [{"distance": distance, "duration": 70000.0}],
                    "way_points": [0, len(points) - 1],
                    "extras": {"fixture": {"values": [[0, len(points) - 1, 4]]}},
                },
            }
        ],
        "metadata": {
            "service": "routing",
            "attribution": "Synthetic response for local tests",
            "query": {"coordinates": [points[0], points[-1]], "units": "m"},
            "engine": {"version": "test-engine"},
        },
    }


def catalog(stations=None):
    stations = (
        stations
        if stations is not None
        else tuple(
            Station(
                station_id,
                35.0,
                longitude,
                Decimal(price),
                record,
                "representative_place_internal_point",
            )
            for record, (station_id, longitude, price) in enumerate(
                (
                    ("A", -116.5, "4"),
                    ("B", -115.5, "3"),
                    ("C", -112.0, "3.5"),
                    ("D", -111.0, "4"),
                ),
                2,
            )
        )
    )
    return StationCatalog(
        stations=stations,
        rows=(),
        exclusions=(),
        source_columns=(),
        source_sha256="synthetic-source",
        output_sha256="synthetic-output",
        report_sha256="synthetic-report",
        price_policy=PricePolicy.LOWEST_LISTED,
        fingerprint=hashlib.sha256(repr(stations).encode()).hexdigest(),
    )


def route_measure(miles):
    """A declared model, independent of projection or external routing services."""
    total = Fraction(miles) * METERS_PER_MILE
    return RouteMeasure(
        fingerprint="synthetic-route",
        coordinates=((0.0, 0.0), (1.0, 0.0)),
        segment_lengths_meters=(total,),
        cumulative_meters=(Fraction(0), total),
        total_distance_meters=total,
        distance_scale=Fraction(1),
        projection_center=(0.5, 0.0),
        original_geojson="{}",
    )


def position(miles, *, measure):
    progress = Fraction(miles)
    fraction = progress / measure.total_miles if measure.total_miles else Fraction(0)
    return RoutePosition(
        measure.fingerprint,
        segment_index=0,
        fraction=fraction,
        progress_miles=progress,
        coordinates=(float(fraction), 0.0),
    )


def visit(station_id, miles, outward, returning, *, measure, price):
    attachment = position(miles, measure=measure)
    return StationVisit(
        station_id=station_id,
        position=attachment,
        coordinates=attachment.coordinates,
        price_per_gallon=Decimal(str(price)),
        outbound_miles=Fraction(outward),
        return_miles=Fraction(returning),
    )
