"""
Uniform error envelope (spec §5):

    {"error": {"code": "string", "message": "human readable", "fields": {...}?}}

All API errors, including DRF's own (authentication, permission, 404, throttling, validation)
are converted here so the Android client only has to parse one shape.
"""
from __future__ import annotations

import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404, JsonResponse
from rest_framework import exceptions, status
from rest_framework.response import Response

logger = logging.getLogger("patroliq.errors")


class ApiError(exceptions.APIException):
    """Raise with an explicit HTTP status and machine-readable ``code``."""

    def __init__(self, status_code: int, code: str, message: str, fields: dict | None = None, headers=None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.fields = fields
        self.headers = headers or {}
        super().__init__(detail=message, code=code)


def error_body(code: str, message: str, fields=None) -> dict:
    body = {"code": code, "message": message}
    if fields:
        body["fields"] = fields
    return {"error": body}


def _collect_codes(codes) -> set[str]:
    found: set[str] = set()
    if isinstance(codes, dict):
        for v in codes.values():
            found |= _collect_codes(v)
    elif isinstance(codes, (list, tuple)):
        for v in codes:
            found |= _collect_codes(v)
    elif isinstance(codes, str):
        found.add(codes)
    return found


def _plain(detail):
    """ErrorDetail trees -> plain JSON (lists of strings)."""
    if isinstance(detail, dict):
        return {k: _plain(v) for k, v in detail.items()}
    if isinstance(detail, (list, tuple)):
        return [_plain(v) for v in detail]
    return str(detail)


def first_message(detail) -> str:
    if isinstance(detail, dict):
        for k, v in detail.items():
            msg = first_message(v)
            return msg if k in ("non_field_errors", "detail") else f"{k}: {msg}"
        return "Invalid input."
    if isinstance(detail, (list, tuple)):
        return first_message(detail[0]) if detail else "Invalid input."
    return str(detail)


def validation_code(detail) -> str:
    """``unexpected_fields`` wins over the generic ``validation_error``."""
    codes = _collect_codes(detail.get_codes() if hasattr(detail, "get_codes") else _codes_of(detail))
    return "unexpected_fields" if "unexpected_fields" in codes else "validation_error"


def _codes_of(detail):
    if isinstance(detail, dict):
        return {k: _codes_of(v) for k, v in detail.items()}
    if isinstance(detail, (list, tuple)):
        return [_codes_of(v) for v in detail]
    return getattr(detail, "code", "invalid")


_DEFAULT_CODES = {
    exceptions.NotAuthenticated: ("not_authenticated", status.HTTP_401_UNAUTHORIZED),
    exceptions.AuthenticationFailed: ("authentication_failed", status.HTTP_401_UNAUTHORIZED),
    exceptions.PermissionDenied: ("permission_denied", status.HTTP_403_FORBIDDEN),
    exceptions.NotFound: ("not_found", status.HTTP_404_NOT_FOUND),
    exceptions.MethodNotAllowed: ("method_not_allowed", status.HTTP_405_METHOD_NOT_ALLOWED),
    exceptions.NotAcceptable: ("not_acceptable", status.HTTP_406_NOT_ACCEPTABLE),
    exceptions.UnsupportedMediaType: ("unsupported_media_type", status.HTTP_415_UNSUPPORTED_MEDIA_TYPE),
    exceptions.Throttled: ("throttled", status.HTTP_429_TOO_MANY_REQUESTS),
    exceptions.ParseError: ("parse_error", status.HTTP_400_BAD_REQUEST),
}


def api_exception_handler(exc, context):
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()

    from rest_framework.views import exception_handler as drf_exception_handler  # lazy: avoids import cycle

    response = drf_exception_handler(exc, context)
    if response is None:
        return None  # unhandled -> Django 500 (JSON via patroliq.urls.handler500)

    if isinstance(exc, ApiError):
        response.data = error_body(exc.code, exc.message, exc.fields)
        for k, v in exc.headers.items():
            response[k] = v
        return response

    if isinstance(exc, exceptions.ValidationError):
        detail = exc.detail if isinstance(exc.detail, dict) else {"non_field_errors": exc.detail}
        code = validation_code(detail)
        message = "Unexpected fields in request." if code == "unexpected_fields" else first_message(detail)
        response.data = error_body(code, message, _plain(detail))
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response

    code, _ = next(((c, s) for cls, (c, s) in _DEFAULT_CODES.items() if isinstance(exc, cls)), ("error", None))
    detail = getattr(exc, "detail", None)
    detail_code = getattr(detail, "code", None)
    # AuthenticationFailed raised with an explicit code (e.g. token_expired) keeps it.
    if detail_code and detail_code not in {"not_authenticated", "authentication_failed", "permission_denied",
                                           "not_found", "method_not_allowed", "throttled", "parse_error",
                                           "invalid", "error", "unsupported_media_type", "not_acceptable"}:
        code = detail_code
    message = str(detail) if detail is not None else "Error."
    response.data = error_body(code, message)
    return response


def json_404(request, exception=None):
    return JsonResponse(error_body("not_found", "Not found."), status=404)


def json_500(request):
    return JsonResponse(error_body("server_error", "Internal server error."), status=500)


def error_response(status_code: int, code: str, message: str, fields=None) -> Response:
    return Response(error_body(code, message, fields), status=status_code)
