import json

from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .services.geocoding import (
    LocationNotFound,
    ProviderError,
    normalize_query,
    resolver,
)


def error_response(code, message, status, *, field=None, **details):
    error = {"code": code, "message": message}
    if field:
        error["field"] = field
    error.update(details)
    return JsonResponse({"error": error}, status=status)


# This read-only JSON API neither uses cookie authentication nor changes user data.
@csrf_exempt
def resolve_locations(request):
    if request.method != "POST":
        response = error_response(
            "method_not_allowed", "Use POST with a JSON body.", 405
        )
        response["Allow"] = "POST"
        return response
    if request.content_type != "application/json":
        return error_response(
            "unsupported_media_type", "Use Content-Type: application/json.", 415
        )
    try:
        body = request.body
        if len(body) > 8192:
            raise RequestDataTooBig
        payload = json.loads(body)
    except RequestDataTooBig:
        return error_response(
            "payload_too_large", "Request body must be at most 8 KB.", 413
        )
    except ValueError, UnicodeError, RecursionError:
        return error_response("invalid_json", "Provide a valid JSON object.", 400)
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
            details = {"reason": exc.reason}
            if exc.provider_status is not None:
                details["provider_status"] = exc.provider_status
            if exc.retry_after is not None:
                details["retry_after"] = exc.retry_after
            response = error_response(
                "geocoding_unavailable", str(exc), exc.status, field=field, **details
            )
            if exc.retry_after is not None:
                response["Retry-After"] = str(exc.retry_after)
            return response
    return JsonResponse(
        {
            **locations,
            "provider": "nominatim",
            "attribution": "© OpenStreetMap contributors",
            "attribution_url": "https://www.openstreetmap.org/copyright",
        }
    )
