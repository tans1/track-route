from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.common.http import error_response, provider_error_response, read_json_post

from .services.geocoding import (
    LocationNotFound,
    ProviderError,
    normalize_query,
    resolver,
)


# This read-only JSON API neither uses cookie authentication nor changes user data.
@csrf_exempt
def resolve_locations(request):
    payload, error = read_json_post(request)
    if error is not None:
        return error
    if not isinstance(payload, dict) or set(payload) != {"start", "destination"}:
        return error_response(
            "invalid_fields", "Provide exactly start and destination.", 400
        )

    # Validate both inputs before issuing either provider request.
    queries = {}
    for field in ("start", "destination"):
        try:
            queries[field] = normalize_query(payload[field])
        except ValueError as exc:
            return error_response("invalid_location", str(exc), 400, field=field)

    locations = {}
    for field, query in queries.items():
        try:
            locations[field] = resolver.resolve(query)
        except LocationNotFound as exc:
            return error_response("location_not_found", str(exc), 422, field=field)
        except ProviderError as exc:
            return provider_error_response(exc, "geocoding_unavailable", field=field)
    return JsonResponse(
        {
            **locations,
            "provider": "nominatim",
            "attribution": "© OpenStreetMap contributors",
            "attribution_url": "https://www.openstreetmap.org/copyright",
        }
    )
