"""Forward route travel with explicit outward and return station excursions.

The unrestricted movement relation is shortcut-closed. Tank capacity filters
neighbors, never the catalog or the underlying distance relation.
"""

from bisect import bisect_left, bisect_right
from fractions import Fraction
from typing import Protocol

from apps.fuel_planning.types import (
    LegDistance,
    RouteMeasure,
    RoutePosition,
    RouteSource,
    StationVisit,
    VehicleSpec,
)

from .numeric import meters_to_miles, validate_fuel


class MovementNotPermitted(ValueError):
    """The requested leg moves backward in the declared route model."""


class DistanceProvider(Protocol):
    """Nonnegative, shortcut-closed distances for the continuous-fuel solver.

    Every permitted two-leg bypass must also exist and cost no more distance.
    A provider that cannot establish this must not claim shortcut_closed.
    """

    shortcut_closed: bool
    destination: RoutePosition
    visits: tuple[StationVisit, ...]
    vehicle: VehicleSpec

    def leg(
        self, origin: RouteSource | StationVisit, target: StationVisit | RoutePosition
    ) -> LegDistance: ...

    def source_neighbors(
        self, source: RouteSource, initial_fuel
    ) -> tuple[StationVisit, ...]: ...

    def full_tank_neighbors(self, visit: StationVisit) -> tuple[StationVisit, ...]: ...


class RouteExcursionDistanceProvider:
    shortcut_closed = True

    def __init__(
        self,
        measure: RouteMeasure,
        visits: tuple[StationVisit, ...],
        vehicle: VehicleSpec | None = None,
    ):
        self.measure = measure
        self.vehicle = vehicle if vehicle is not None else VehicleSpec()
        self.visits = tuple(visits)
        self._visits_by_id = {}
        for visit in self.visits:
            self._check_position(visit.position)
            if visit.visit_id in self._visits_by_id:
                raise ValueError("Station visit identities must be unique.")
            self._visits_by_id[visit.visit_id] = visit
        self._ordered = tuple(
            sorted(
                self.visits,
                key=lambda visit: (visit.position.progress_miles, visit.visit_id),
            )
        )
        self._progress = tuple(visit.position.progress_miles for visit in self._ordered)
        self.destination = RoutePosition(
            route_id=measure.fingerprint,
            segment_index=len(measure.coordinates) - 2,
            fraction=Fraction(1),
            progress_miles=measure.total_miles,
            coordinates=measure.coordinates[-1],
        )

    def _check_position(self, position: RoutePosition):
        if position.route_id != self.measure.fingerprint:
            raise ValueError("Position belongs to another route.")
        index = position.segment_index
        if index >= len(self.measure.segment_lengths_meters):
            raise ValueError("Position segment is outside the route.")
        expected_progress = meters_to_miles(
            (
                self.measure.cumulative_meters[index]
                + self.measure.segment_lengths_meters[index] * position.fraction
            )
            * self.measure.distance_scale
        )
        if position.progress_miles != expected_progress:
            raise ValueError("Position progress does not match its route segment.")
        if not 0 <= position.progress_miles <= self.measure.total_miles:
            raise ValueError("Position is outside the route's distance range.")

    def _known_visit(self, visit: StationVisit) -> StationVisit:
        if self._visits_by_id.get(visit.visit_id) != visit:
            raise ValueError("Station visit is not part of this movement model.")
        return visit

    def _source_visit(self, source: RouteSource) -> StationVisit | None:
        self._check_position(source.position)
        if source.station_visit_id is None:
            if source.coordinates != source.position.coordinates:
                raise ValueError("An on-route source must be at its route position.")
            return None
        visit = self._visits_by_id.get(source.station_visit_id)
        if (
            visit is None
            or source.position != visit.position
            or source.coordinates != visit.coordinates
        ):
            raise ValueError(
                "Source pump context does not match a known station visit."
            )
        return visit

    @staticmethod
    def _identity(point: RouteSource | StationVisit | RoutePosition) -> str:
        if isinstance(point, StationVisit):
            return repr(point.visit_id)
        if isinstance(point, RouteSource):
            return (
                repr(point.station_visit_id)
                if point.station_visit_id
                else f"source:{point.position.attachment_id}"
            )
        return f"route:{point.attachment_id}"

    def leg(
        self, origin: RouteSource | StationVisit, target: StationVisit | RoutePosition
    ) -> LegDistance:
        if isinstance(origin, RouteSource):
            departure = self._source_visit(origin)
        elif isinstance(origin, StationVisit):
            departure = self._known_visit(origin)
        else:
            raise TypeError("Leg origin must be a source or station visit.")
        if isinstance(target, StationVisit):
            arrival = self._known_visit(target)
            target_position = target.position
        elif isinstance(target, RoutePosition):
            self._check_position(target)
            arrival = None
            target_position = target
        else:
            raise TypeError("Leg target must be a station visit or route position.")
        delta = target_position.progress_miles - origin.position.progress_miles
        if delta < 0:
            raise MovementNotPermitted(
                "Travel to earlier route progress is not permitted."
            )

        # A traveler already at this pump can purchase before paying any return.
        if departure is not None and arrival == departure:
            miles = Fraction()
            quality = "station_identity"
        else:
            miles = delta
            connectors = []
            if departure is not None:
                miles += departure.return_miles
                if departure.return_miles:
                    connectors.append(departure.distance_quality)
            if arrival is not None:
                miles += arrival.outbound_miles
                if arrival.outbound_miles:
                    connectors.append(arrival.distance_quality)
            quality = (
                "ors_baseline"
                if not connectors
                else connectors[0]
                if len(set(connectors)) == 1
                else "mixed_access"
            )
        return LegDistance(
            miles=miles,
            origin_id=self._identity(origin),
            destination_id=self._identity(target),
            distance_quality=quality,
            movement_model=self.measure.model_version,
        )

    def _neighbors(
        self, origin: RouteSource | StationVisit, available_miles: Fraction
    ) -> tuple[StationVisit, ...]:
        departure = (
            self._source_visit(origin)
            if isinstance(origin, RouteSource)
            else self._known_visit(origin)
        )
        progress = origin.position.progress_miles
        return_miles = departure.return_miles if departure is not None else Fraction()
        upper = progress + available_miles - return_miles
        candidates = []
        if upper >= progress:
            start = bisect_left(self._progress, progress)
            end = bisect_right(self._progress, upper)
            candidates = [
                visit
                for visit in self._ordered[start:end]
                if visit != departure
                # Exact rearrangement of b + (s_j - s_i) + a_j <= range.
                # Reject inaccessible visits before constructing leg metadata
                # or hashing identities; every catalog visit remains indexed.
                and visit.position.progress_miles + visit.outbound_miles <= upper
            ]
        if isinstance(origin, RouteSource) and departure is not None:
            # This zero-distance purchase opportunity survives even if the
            # pump's outgoing return cannot be driven with current fuel.
            candidates.append(departure)
            candidates.sort(
                key=lambda visit: (visit.position.progress_miles, visit.visit_id)
            )
        return tuple(candidates)

    def source_neighbors(
        self, source: RouteSource, initial_fuel
    ) -> tuple[StationVisit, ...]:
        fuel = validate_fuel(initial_fuel, self.vehicle.tank_capacity_gallons)
        return self._neighbors(source, fuel * self.vehicle.mpg)

    def full_tank_neighbors(self, visit: StationVisit) -> tuple[StationVisit, ...]:
        return self._neighbors(visit, self.vehicle.maximum_range_miles)
