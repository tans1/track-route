"""JSON request parsing and error responses for the trip API."""

import json

from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse


def error_response(code, message, status, *, field=None, **details):
    error = {"code": code, "message": message}
    if field:
        error["field"] = field
    error.update(details)
    return JsonResponse({"error": error}, status=status)


def read_json_post(request, *, parse_float=float):
    if request.method != "POST":
        response = error_response(
            "method_not_allowed", "Use POST with a JSON body.", 405
        )
        response["Allow"] = "POST"
        return None, response
    if request.content_type != "application/json":
        return None, error_response(
            "unsupported_media_type", "Use Content-Type: application/json.", 415
        )
    try:
        body = request.body
        if len(body) > 8192:
            raise RequestDataTooBig
        return json.loads(body, parse_float=parse_float), None
    except RequestDataTooBig:
        return None, error_response(
            "payload_too_large", "Request body must be at most 8 KB.", 413
        )
    except ValueError, UnicodeError, RecursionError:
        return None, error_response("invalid_json", "Provide a valid JSON object.", 400)


def provider_error_response(error, code, *, field=None):
    details = {"reason": error.reason}
    if error.provider_status is not None:
        details["provider_status"] = error.provider_status
    if error.retry_after is not None:
        details["retry_after"] = error.retry_after
    response = error_response(code, str(error), error.status, field=field, **details)
    if error.retry_after is not None:
        response["Retry-After"] = str(error.retry_after)
    return response
