"""
One error shape for the whole public API, and a fixed vocabulary of codes.

Every failure a developer can hit comes back as::

    {"success": false, "error": {"code": "INVALID_RECIPIENT", "message": "..."}}

with the HTTP status that matches. Two rules make that worth relying on:

* the **code** is the contract and the **message** is not. Code strings never
  change once published; messages are prose and are improved freely. A client
  branching on the sentence is a client that breaks when somebody fixes a typo,
  which is why the code exists.
* **nothing escapes this shape.** DRF's own exceptions (validation, 404,
  method-not-allowed, throttling) and unhandled Python exceptions are both
  translated here, so a developer never sees `{"detail": ...}` from one
  endpoint and this from the next. That translation is installed per-view via
  ``PublicAPIViewMixin.get_exception_handler`` -- deliberately *not* by
  changing the project-wide DRF setting, which would rewrite every internal
  endpoint the SPA depends on.
"""

import logging

from django.http import Http404
from rest_framework import status as http_status
from rest_framework.exceptions import (
    APIException,
    AuthenticationFailed,
    MethodNotAllowed,
    NotAuthenticated,
    NotFound,
    ParseError,
    PermissionDenied,
    Throttled,
    ValidationError,
)
from rest_framework.response import Response

from core.request_context import get_request_id

logger = logging.getLogger("sendstack")


# --- The code vocabulary -------------------------------------------------

INVALID_API_KEY = "INVALID_API_KEY"
EXPIRED_API_KEY = "EXPIRED_API_KEY"
REVOKED_API_KEY = "REVOKED_API_KEY"
DISABLED_API_KEY = "DISABLED_API_KEY"
INSUFFICIENT_PERMISSION = "INSUFFICIENT_PERMISSION"
ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
ACCOUNT_DISCONNECTED = "ACCOUNT_DISCONNECTED"
INVALID_RECIPIENT = "INVALID_RECIPIENT"
INSUFFICIENT_CREDITS = "INSUFFICIENT_CREDITS"
RATE_LIMITED = "RATE_LIMITED"
MESSAGE_FAILED = "MESSAGE_FAILED"
INVALID_TEMPLATE = "INVALID_TEMPLATE"
INVALID_REQUEST = "INVALID_REQUEST"
NOT_FOUND = "NOT_FOUND"
METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
CAMPAIGN_STATE_INVALID = "CAMPAIGN_STATE_INVALID"
TEST_KEY_CANNOT_SEND = "TEST_KEY_CANNOT_SEND"
WORKSPACE_SUSPENDED = "WORKSPACE_SUSPENDED"
HTTPS_REQUIRED = "HTTPS_REQUIRED"
INTERNAL_ERROR = "INTERNAL_ERROR"

#: Documented so the OpenAPI schema and the docs page can list them without
#: repeating the set, and so a test can assert nothing is raised that is not
#: published here.
ERROR_CODES = {
    INVALID_API_KEY: "The API key is missing, malformed, or does not exist.",
    EXPIRED_API_KEY: "The API key passed its expiry date.",
    REVOKED_API_KEY: "The API key was revoked and can never be used again.",
    DISABLED_API_KEY: "The API key is currently disabled. Re-enable it in the dashboard.",
    INSUFFICIENT_PERMISSION: "The API key does not hold the scope this endpoint requires.",
    ACCOUNT_NOT_FOUND: "No WhatsApp account with that id is available to this key.",
    ACCOUNT_DISCONNECTED: "The WhatsApp account exists but is not connected.",
    INVALID_RECIPIENT: "The recipient phone number could not be used.",
    INSUFFICIENT_CREDITS: "The workspace does not have enough credits for this send.",
    RATE_LIMITED: "The key exceeded one of its rate limits. Retry after the reset.",
    MESSAGE_FAILED: "The message could not be queued.",
    INVALID_TEMPLATE: "The template does not exist, is inactive, or its variables do not match.",
    INVALID_REQUEST: "The request body failed validation.",
    NOT_FOUND: "No such object in this workspace.",
    METHOD_NOT_ALLOWED: "That HTTP method is not supported on this endpoint.",
    IDEMPOTENCY_CONFLICT: "This Idempotency-Key was already used for a different request body.",
    IDEMPOTENCY_IN_PROGRESS: "An identical request with this Idempotency-Key is still being processed.",
    QUOTA_EXCEEDED: "The workspace's plan allowance for this action is exhausted.",
    CAMPAIGN_STATE_INVALID: "The campaign is not in a state that allows this transition.",
    TEST_KEY_CANNOT_SEND: "Test-environment keys cannot send real messages.",
    WORKSPACE_SUSPENDED: "The workspace is suspended.",
    HTTPS_REQUIRED: "The public API refuses plaintext HTTP in this deployment.",
    INTERNAL_ERROR: "Something went wrong on our side. The error has been recorded.",
}


class PublicAPIError(APIException):
    """
    Any refusal a public endpoint raises deliberately.

    Carries the code and the HTTP status together so a call site states both in
    one place, and so `error_code_of` can recover the code from the exception
    without re-deriving it from the status.
    """

    status_code = http_status.HTTP_400_BAD_REQUEST
    default_code = INVALID_REQUEST

    def __init__(self, code=None, message=None, *, status_code=None, extra=None):
        self.code = code or self.default_code
        self.message = message or ERROR_CODES.get(self.code, "Request failed.")
        if status_code is not None:
            self.status_code = status_code
        #: Merged into the `error` object. Used for the machine-readable
        #: details a client can act on -- which field failed, when to retry.
        self.extra = extra or {}
        super().__init__(self.message)


def error_response(code, message=None, *, status_code=400, extra=None, headers=None):
    """Build the canonical failure body. The single place its shape is decided."""
    body = {
        "success": False,
        "error": {
            "code": code,
            "message": message or ERROR_CODES.get(code, "Request failed."),
        },
        "request_id": get_request_id(),
    }
    if extra:
        body["error"].update(extra)
    return Response(body, status=status_code, headers=headers or {})


#: DRF exception -> (code, http status). Anything not listed is handled by the
#: fall-through below, so adding a DRF exception type here is a refinement
#: rather than a fix for something currently broken.
_DRF_MAP = {
    NotAuthenticated: (INVALID_API_KEY, 401),
    AuthenticationFailed: (INVALID_API_KEY, 401),
    PermissionDenied: (INSUFFICIENT_PERMISSION, 403),
    NotFound: (NOT_FOUND, 404),
    Http404: (NOT_FOUND, 404),
    MethodNotAllowed: (METHOD_NOT_ALLOWED, 405),
    ParseError: (INVALID_REQUEST, 400),
    ValidationError: (INVALID_REQUEST, 400),
    Throttled: (RATE_LIMITED, 429),
}


def public_api_exception_handler(exc, context):
    """
    The public API's own DRF exception handler.

    Installed per-view rather than globally -- see this module's docstring.
    """
    if isinstance(exc, PublicAPIError):
        return error_response(
            exc.code, exc.message, status_code=exc.status_code, extra=exc.extra,
        )

    for exception_type, (code, status_code) in _DRF_MAP.items():
        if isinstance(exc, exception_type):
            extra = None
            message = None
            if isinstance(exc, ValidationError):
                # DRF's validation detail is a dict of field -> [messages],
                # which is genuinely the most useful thing we can hand back.
                # It goes in `details` rather than replacing `message`, so the
                # top-level shape stays identical for every failure.
                extra = {"details": exc.detail}
                message = "One or more fields are invalid."
            elif isinstance(exc, Throttled) and exc.wait is not None:
                extra = {"retry_after": int(exc.wait)}
            return error_response(
                code, message or _first_message(exc), status_code=status_code, extra=extra,
            )

    if isinstance(exc, APIException):
        return error_response(
            INVALID_REQUEST, _first_message(exc), status_code=exc.status_code,
        )

    # Nothing recognised it. Log the traceback for us; tell the caller nothing
    # about it beyond the request id they can quote at support.
    logger.exception("Unhandled public API exception (request_id=%s)", get_request_id(), exc_info=exc)
    return error_response(INTERNAL_ERROR, status_code=500)


def _first_message(exc) -> str:
    """A single readable sentence out of whatever DRF put in `detail`."""
    detail = getattr(exc, "detail", None)
    if detail is None:
        return str(exc)
    if isinstance(detail, dict):
        for value in detail.values():
            return _first_message_from(value)
        return "Request failed."
    return _first_message_from(detail)


def _first_message_from(value) -> str:
    if isinstance(value, (list, tuple)) and value:
        return str(value[0])
    return str(value)
