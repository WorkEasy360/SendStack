import logging

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .request_context import get_request_id

logger = logging.getLogger("sendstack")


def api_exception_handler(exc, context):
    """Wrap DRF's default handler so unhandled exceptions never leak a traceback."""

    response = drf_exception_handler(exc, context)
    request_id = get_request_id()

    if response is None:
        logger.exception("Unhandled exception (request_id=%s)", request_id, exc_info=exc)
        return Response(
            {
                "detail": "Something went wrong. We've recorded the error. Please try again.",
                "request_id": request_id,
            },
            status=500,
        )

    if isinstance(response.data, dict):
        # An entitlement refusal carries more than a sentence: which feature
        # or which quota, and a stable `code`. DRF only ever renders `detail`,
        # so without this the frontend would be left parsing English to decide
        # whether to offer an upgrade. See apps/subscriptions/entitlements.py.
        extra = getattr(exc, "extra", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                response.data.setdefault(key, value)

        response.data.setdefault("request_id", request_id)

    return response
