import logging

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .request_context import get_request_id

logger = logging.getLogger("multisender")


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
        response.data.setdefault("request_id", request_id)

    return response
