from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.common.http import error_response, provider_error_response, read_json_post
from apps.locations.services.geocoding import (
    LocationNotFound,
    ProviderError,
    normalize_query,
    resolver,
)

from .services.directions import (
    RoutingError,
    directions,
    normalize_options,
    validate_profile,
)


# Read-only JSON API; no cookie authentication or persistent user-data changes.
@csrf_exempt
def fetch_route(request):
    payload, error = read_json_post(request)
    if error is not None:
        return error
    required = {"start", "destination"}
    if (
        not isinstance(payload, dict)
        or not required.issubset(payload)
        or set(payload) - (required | {"profile", "options"})
    ):
        return error_response(
            "invalid_fields",
            "Provide start and destination, with optional profile and options.",
            400,
        )
    queries = {}
    for field in ("start", "destination"):
        try:
            queries[field] = normalize_query(payload[field])
        except ValueError as exc:
            return error_response("invalid_location", str(exc), 400, field=field)
    try:
        profile = validate_profile(payload.get("profile", "driving-car"))
        options = normalize_options(payload.get("options", {}))
    except ValueError as exc:
        return error_response("invalid_route_options", str(exc), 400)
    try:
        # Fail before spending geocoding requests if routing is not configured.
        directions.require_api_key()
    except RoutingError as exc:
        return provider_error_response(exc, "routing_unavailable")

    locations = {}
    for field, query in queries.items():
        try:
            locations[field] = resolver.resolve(query)
        except LocationNotFound as exc:
            return error_response("location_not_found", str(exc), 422, field=field)
        except ProviderError as exc:
            return provider_error_response(exc, "geocoding_unavailable", field=field)
    try:
        route = directions.route(
            [locations["start"]["longitude"], locations["start"]["latitude"]],
            [
                locations["destination"]["longitude"],
                locations["destination"]["latitude"],
            ],
            profile=profile,
            options=options,
        )
    except RoutingError as exc:
        code = (
            "route_not_found"
            if exc.reason == "route_not_found"
            else "routing_unavailable"
        )
        return provider_error_response(exc, code)
    return JsonResponse(
        {
            **locations,
            "profile": profile,
            "provider": "openrouteservice",
            "route": route,
            "geocoding_attribution": "© OpenStreetMap contributors",
            "geocoding_attribution_url": "https://www.openstreetmap.org/copyright",
        },
        json_dumps_params={"allow_nan": False},
    )
