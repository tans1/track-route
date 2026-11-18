"""Application workflow: resolve a named US trip and run the local planner."""

from dataclasses import dataclass

from apps.common.providers import ProviderError
from apps.locations.services.geocoding import LocationNotFound, resolver
from apps.routes.services.directions import RoutingError, directions
from apps.stations.services.catalog import CatalogIntegrityError, StationCatalog

from ..serializers import FuelPlanRequest
from .cache import load_configured_catalog
from .optimizer import NoFeasiblePlan, SearchIncomplete
from .planner import CurrentStationError, TripPlan, plan_trip, validate_current_station
from .route_geometry import ModelGeometryError


@dataclass(frozen=True, slots=True)
class ResolvedTrip:
    start: dict
    destination: dict
    plan: TripPlan
    catalog: StationCatalog


class TripPlanningError(Exception):
    """A planning failure that the HTTP layer can present without local details."""

    def __init__(
        self, code, message, status, *, field=None, provider_error=None, **details
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.field = field
        self.provider_error = provider_error
        self.details = details

    @classmethod
    def from_provider(cls, error, code, *, field=None):
        return cls(code, str(error), error.status, field=field, provider_error=error)


def plan_named_trip(request: FuelPlanRequest) -> ResolvedTrip:
    """Accept a validated FuelPlanRequest; perform at most one ORS request.

    Configuration, data integrity and explicit station identity are checked before
    geocoding. The coordinate planner owns the initial route lookup and all local
    calculations. HTTP responses and map storage belong to the view.
    """
    try:
        directions.require_api_key()
        try:
            catalog = load_configured_catalog()
        except (CatalogIntegrityError, OSError, ValueError) as exc:
            raise TripPlanningError(
                "station_data_unavailable",
                "The station CSV and provenance report must be present and agree.",
                503,
            ) from exc
        validate_current_station(request.current_station_id, catalog)
        locations = {}
        for field in ("start", "destination"):
            try:
                location = resolver.resolve(getattr(request, field))
                if location["country_code"] != "us":
                    raise LocationNotFound(
                        "Both trip locations must be within the USA."
                    )
                locations[field] = location
            except LocationNotFound as exc:
                raise TripPlanningError(
                    "location_not_found", str(exc), 422, field=field
                ) from exc
            except ProviderError as exc:
                raise TripPlanningError.from_provider(
                    exc, "geocoding_unavailable", field=field
                ) from exc
        plan = plan_trip(
            [locations["start"]["longitude"], locations["start"]["latitude"]],
            [
                locations["destination"]["longitude"],
                locations["destination"]["latitude"],
            ],
            initial_fuel_gallons=request.initial_fuel_gallons,
            current_station_id=request.current_station_id,
            profile=request.profile,
            options=request.options,
            catalog=catalog,
            directions_client=directions,
        )
        return ResolvedTrip(**locations, plan=plan, catalog=catalog)
    except RoutingError as exc:
        code = (
            "route_not_found"
            if exc.reason == "route_not_found"
            else "routing_unavailable"
        )
        raise TripPlanningError.from_provider(exc, code) from exc
    except CurrentStationError as exc:
        raise TripPlanningError(
            "invalid_current_station", str(exc), 422, field=exc.field, reason=exc.reason
        ) from exc
    except (NoFeasiblePlan, SearchIncomplete) as exc:
        incomplete = isinstance(exc, SearchIncomplete)
        raise TripPlanningError(
            "search_budget_exhausted" if incomplete else "model_infeasible",
            str(exc),
            503 if incomplete else 422,
        ) from exc
    except ModelGeometryError as exc:
        raise TripPlanningError(
            "route_model_unavailable",
            "The returned route cannot support the local movement model.",
            502,
        ) from exc
