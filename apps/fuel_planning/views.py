"""One request resolves US endpoints and returns a locally calculated fuel plan."""

from decimal import Decimal

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_safe

from apps.common.http import error_response, provider_error_response, read_json_post
from apps.common.metrics import collect_provider_metrics

from .serializers import (
    FuelPlanRequestError,
    parse_fuel_plan_request,
    serialize_trip_response,
)
from .services.cache import get_map_plan, store_map_plan
from .services.map_snapshot import build_map_snapshot
from .services.trips import TripPlanningError, plan_named_trip


# Read-only API with no cookie authentication or persistent user-data changes.
@csrf_exempt
def create_fuel_plan(request):
    with collect_provider_metrics() as metrics:
        response = _create_fuel_plan(request)
    response["X-ORS-Requests"] = str(metrics.provider_calls["ors"])
    response["X-Geocoding-Requests"] = str(metrics.provider_calls["nominatim"])
    response["Cache-Control"] = "no-store"
    return response


def _create_fuel_plan(request):
    payload, error = read_json_post(request, parse_float=Decimal)
    if error is not None:
        return error
    try:
        parsed = parse_fuel_plan_request(payload)
    except FuelPlanRequestError as exc:
        return error_response(exc.code, str(exc), exc.status, field=exc.field)

    try:
        result = plan_named_trip(parsed)
    except TripPlanningError as exc:
        if exc.provider_error is not None:
            return provider_error_response(
                exc.provider_error, exc.code, field=exc.field
            )
        return error_response(
            exc.code, str(exc), exc.status, field=exc.field, **exc.details
        )

    snapshot = build_map_snapshot(result.plan, catalog=result.catalog)
    snapshot.update(start=result.start, destination=result.destination)
    plan_id = store_map_plan(snapshot)
    map_url = request.build_absolute_uri(
        reverse("fuel_planning:map", kwargs={"plan_id": plan_id})
    )
    response = JsonResponse(
        serialize_trip_response(snapshot, map_url=map_url),
        json_dumps_params={"allow_nan": False},
    )
    response["X-Plan-Cache"] = "hit" if result.plan.cache_hit else "miss"
    return response


@require_safe
def fuel_plan_map(request, plan_id):
    plan = get_map_plan(str(plan_id))
    if plan is None:
        response = render(request, "fuel_planning/expired.html", status=410)
    else:
        response = render(
            request,
            "fuel_planning/map.html",
            {
                "plan": plan,
                "tile_url": settings.MAP_TILE_URL,
                "tile_attribution": settings.MAP_TILE_ATTRIBUTION,
            },
        )
    response["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response["Cache-Control"] = "no-store"
    return response
