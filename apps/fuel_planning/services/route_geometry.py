"""Attach every station to an unsimplified route in a declared local model.

Projection is geometric, not evidence of a legal exit or a station entrance.
All distances returned to the model are exact fractions of declared decimals.
"""

import bisect
import hashlib
import json
import math
from dataclasses import replace
from fractions import Fraction
from itertools import pairwise

from pyproj import CRS, Geod, Transformer
from pyproj.exceptions import ProjError
from shapely import LineString, Point, STRtree

from apps.fuel_planning.types import (
    MOVEMENT_MODEL_VERSION,
    RouteMeasure,
    RoutePosition,
    RouteSource,
    StationVisit,
)

from .numeric import as_exact_amount, coordinates, meters_to_miles, nonnegative_amount

GEOMETRY_VERSION = "wgs84-aeqd-segments-v1"
GEOD = Geod(ellps="WGS84")
# Geometry tolerances only; never used for fuel reachability or cost comparison.
ATTACHMENT_TIE_METERS = 0.000001
SOURCE_TOLERANCE_METERS = 0.01


class ModelGeometryError(ValueError):
    """The geometry cannot be represented by the declared movement model."""


class SourceLocationError(ModelGeometryError):
    """A source is off route or its explicit context does not match."""


class SourceAmbiguityError(SourceLocationError):
    def __init__(self, candidates):
        super().__init__(
            "Source has multiple route occurrences; provide explicit context."
        )
        self.candidates = tuple(candidates)


def geodesic_distance_meters(first, second):
    if first == second:
        return Fraction()
    distance = GEOD.inv(*first, *second)[2]
    if not math.isfinite(distance) or distance < 0:
        raise ModelGeometryError("Cannot measure the geographic coordinates.")
    return as_exact_amount(distance)


def _projection_center(points):
    vectors = [
        (
            math.cos(math.radians(lat)) * math.cos(math.radians(lon)),
            math.cos(math.radians(lat)) * math.sin(math.radians(lon)),
            math.sin(math.radians(lat)),
        )
        for lon, lat in points
    ]
    x, y, z = (math.fsum(vector[index] for vector in vectors) for index in range(3))
    if math.sqrt(x * x + y * y + z * z) < len(points) * 1e-12:
        raise ModelGeometryError("Route has no unambiguous projection center.")
    return math.degrees(math.atan2(y, x)), math.degrees(math.atan2(z, math.hypot(x, y)))


class _RouteIndex:
    def __init__(self, measure):
        self.measure = measure
        lon, lat = measure.projection_center
        local = CRS.from_proj4(
            f"+proj=aeqd +lon_0={lon} +lat_0={lat} +datum=WGS84 +units=m +no_defs"
        )
        self.forward = Transformer.from_crs("EPSG:4326", local, always_xy=True)
        self.inverse = Transformer.from_crs(local, "EPSG:4326", always_xy=True)
        self.points = tuple(self.project(point) for point in measure.coordinates)
        self.segments = tuple(
            Point(start) if start == end else LineString((start, end))
            for start, end in pairwise(self.points)
        )
        self.tree = STRtree(self.segments)

    def project(self, point):
        # AEQD is singular at the antipode. A hemisphere is the explicit model
        # domain: it covers US cross-country/Alaska/Hawaii cases without silently
        # wrapping distant geometry through that singularity.
        lon, lat = map(math.radians, point)
        center_lon, center_lat = map(math.radians, self.measure.projection_center)
        dot = math.sin(lat) * math.sin(center_lat) + math.cos(lat) * math.cos(
            center_lat
        ) * math.cos(lon - center_lon)
        if dot <= 0:
            raise ModelGeometryError(
                "Coordinates lie outside the route projection hemisphere."
            )
        try:
            projected = self.forward.transform(*point, errcheck=True)
        except ProjError as exc:
            raise ModelGeometryError("Coordinate projection failed.") from exc
        if not all(math.isfinite(value) for value in projected):
            raise ModelGeometryError("Coordinate projection is not finite.")
        return projected

    def position(self, index, fraction):
        fraction = as_exact_amount(fraction)
        if type(index) is not int or not 0 <= index < len(self.segments):
            raise SourceLocationError("Source segment index is outside the route.")
        if not 0 <= fraction <= 1:
            raise SourceLocationError(
                "Source interpolation fraction is outside its segment."
            )
        raw_progress = (
            self.measure.cumulative_meters[index]
            + self.measure.segment_lengths_meters[index] * fraction
        )
        # Canonicalize a shared vertex and repeated zero-length segments at the
        # same progress. Revisited coordinates at different progress stay distinct.
        if fraction in (0, 1) or self.measure.segment_lengths_meters[index] == 0:
            vertex = bisect.bisect_left(self.measure.cumulative_meters, raw_progress)
            point = self.measure.coordinates[vertex]
            index, fraction = (
                (0, Fraction()) if vertex == 0 else (vertex - 1, Fraction(1))
            )
        else:
            projected = self.segments[index].interpolate(
                float(fraction), normalized=True
            )
            try:
                point = coordinates(
                    self.inverse.transform(projected.x, projected.y, errcheck=True)
                )
            except (ProjError, ValueError, TypeError, OverflowError) as exc:
                raise ModelGeometryError(
                    "Attachment inverse projection failed."
                ) from exc
        return RoutePosition(
            self.measure.fingerprint,
            index,
            fraction,
            meters_to_miles(raw_progress * self.measure.distance_scale),
            point,
        )

    def nearest(self, point):
        projected = Point(self.project(point))
        _, distances = self.tree.query_nearest(
            projected, all_matches=True, return_distance=True
        )
        minimum = float(min(distances))
        # Include numerical ties so reversed overlapping segments do not lose an
        # occurrence due to floating-point ordering in the spatial index.
        indices = self.tree.query(
            projected, predicate="dwithin", distance=minimum + ATTACHMENT_TIE_METERS
        )
        by_progress = {}
        for index in sorted(int(value) for value in indices):
            segment = self.segments[index]
            if segment.distance(projected) > minimum + ATTACHMENT_TIE_METERS:
                continue
            if segment.geom_type == "Point":
                fraction = 0.0
            else:
                along = segment.project(projected)
                if along <= ATTACHMENT_TIE_METERS:
                    fraction = 0.0
                elif segment.length - along <= ATTACHMENT_TIE_METERS:
                    fraction = 1.0
                else:
                    fraction = along / segment.length
            position = self.position(index, fraction)
            if (
                minimum <= ATTACHMENT_TIE_METERS
                and geodesic_distance_meters(position.coordinates, point)
                <= ATTACHMENT_TIE_METERS
            ):
                # An on-route point must not acquire a positive excursion solely
                # from inverse-projection roundoff. Canonicalize geometry here;
                # all later reachability comparisons remain exact.
                position = replace(position, coordinates=point)
            by_progress.setdefault(position.progress_miles, position)
        return tuple(by_progress[key] for key in sorted(by_progress)), minimum

    def validate_position(self, position):
        if position.route_id != self.measure.fingerprint:
            raise SourceLocationError("Source context belongs to another route.")
        expected = self.position(position.segment_index, position.fraction)
        if position.progress_miles != expected.progress_miles or (
            geodesic_distance_meters(position.coordinates, expected.coordinates)
            > SOURCE_TOLERANCE_METERS
        ):
            raise SourceLocationError(
                "Source context does not agree with the route geometry."
            )


def build_route_measure(geojson) -> RouteMeasure:
    """Measure the first ORS route while preserving its complete response.

    Repeated points are retained. A zero route must have zero reported distance
    and zero measured geometry; an inconsistent response is an explicit error.
    The fingerprint includes full metadata, reported distance and model version.
    """
    try:
        if not isinstance(geojson, dict) or geojson.get("type") != "FeatureCollection":
            raise ModelGeometryError("Expected an ORS GeoJSON FeatureCollection.")
        original = json.dumps(
            geojson, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        feature = geojson["features"][0]
        geometry = feature["geometry"]
        if geometry["type"] != "LineString" or len(geometry["coordinates"]) < 2:
            raise ModelGeometryError(
                "A route needs a LineString with at least two positions."
            )
        points = []
        for point in geometry["coordinates"]:
            if not isinstance(point, (tuple, list)) or len(point) not in (2, 3):
                raise ModelGeometryError(
                    "Route positions require longitude and latitude."
                )
            points.append(coordinates(point[:2]))
        reported = nonnegative_amount(feature["properties"]["summary"]["distance"])
    except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, ModelGeometryError):
            raise
        raise ModelGeometryError("Malformed ORS route geometry or distance.") from exc
    points = tuple(points)
    lengths = tuple(
        geodesic_distance_meters(first, second) for first, second in pairwise(points)
    )
    cumulative = [Fraction()]
    for length in lengths:
        cumulative.append(cumulative[-1] + length)
    total = cumulative[-1]
    if bool(total) != bool(reported):
        raise ModelGeometryError(
            "Reported distance and measurable route geometry disagree at zero."
        )
    identity = f"{MOVEMENT_MODEL_VERSION}:{GEOMETRY_VERSION}:{original}"
    measure = RouteMeasure(
        fingerprint=hashlib.sha256(identity.encode()).hexdigest(),
        coordinates=points,
        segment_lengths_meters=lengths,
        cumulative_meters=tuple(cumulative),
        total_distance_meters=reported,
        distance_scale=reported / total if total else Fraction(1),
        projection_center=_projection_center(points),
        original_geojson=original,
    )
    _RouteIndex(measure)  # Validate the entire route's projection domain now.
    return measure


def attach_stations(catalog, route_measure) -> tuple[StationVisit, ...]:
    """Attach every eligible station, sharing geometry work but never identities."""
    index = _RouteIndex(route_measure)
    attachments = {}
    visits = []
    for station in catalog.eligible_stations:
        point = coordinates((station.longitude, station.latitude))
        if point not in attachments:
            positions, _ = index.nearest(point)
            attachments[point] = tuple(
                (
                    position,
                    meters_to_miles(
                        geodesic_distance_meters(position.coordinates, point)
                    ),
                )
                for position in positions
            )
        for position, connector in attachments[point]:
            visits.append(
                StationVisit(
                    station_id=station.station_id,
                    position=position,
                    coordinates=point,
                    price_per_gallon=station.price_per_gallon,
                    outbound_miles=connector,
                    return_miles=connector,
                    coordinate_quality=station.coordinate_quality,
                    source_csv_record=station.source_csv_record,
                )
            )
    return tuple(
        sorted(
            visits, key=lambda visit: (visit.position.progress_miles, visit.visit_id)
        )
    )


def locate_source(point, route_measure, context=None) -> RouteSource:
    """Locate an on-route point, or explicitly identify a current station visit."""
    point = coordinates(point)
    index = _RouteIndex(route_measure)
    if isinstance(context, StationVisit):
        index.validate_position(context.position)
        if (
            geodesic_distance_meters(point, context.coordinates)
            > SOURCE_TOLERANCE_METERS
        ):
            raise SourceLocationError(
                "Source coordinates do not match the specified station visit."
            )
        return RouteSource.at_station(context)
    if isinstance(context, RoutePosition):
        index.validate_position(context)
        if (
            geodesic_distance_meters(point, context.coordinates)
            > SOURCE_TOLERANCE_METERS
        ):
            raise SourceLocationError(
                "Source coordinates do not match the specified route position."
            )
        return RouteSource(context)
    if context is not None:
        raise TypeError("Source context must be a RoutePosition or StationVisit.")
    positions, distance = index.nearest(point)
    if distance > SOURCE_TOLERANCE_METERS:
        raise SourceLocationError(
            "Source is off route; explicit access context is required."
        )
    if len(positions) != 1:
        raise SourceAmbiguityError(positions)
    return RouteSource(positions[0])
