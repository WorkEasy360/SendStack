"""
The base every public endpoint inherits, and the cross-cutting work it does.

A public view gets, without writing a line of it:

* API-key authentication and nothing else -- no session, no JWT, no CSRF path;
* default-deny scope checking against its `required_scopes`;
* per-key rate limiting with `X-RateLimit-*` on every response, refusals
  included;
* the public error envelope for every exception, DRF's and Python's alike;
* an `ApiRequestLog` row and a `last_used_at` stamp;
* HTTPS enforcement in production.

Doing this in a base class rather than middleware is deliberate. Middleware
would have to guess which requests are public-API requests by inspecting the
path, and would run for the SPA's endpoints too; inheritance makes the answer
structural. It also means `dispatch` is the single funnel -- there is no way to
add an endpoint to this app that quietly skips the logging or the rate limit.
"""

import logging
import time

from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import APIView

from core.request_context import get_request_id

from .. import throttling
from ..authentication import ApiKeyAuthentication, record_usage
from ..errors import (
    HTTPS_REQUIRED,
    PublicAPIError,
    error_response,
    public_api_exception_handler,
)
from ..models import ApiRequestLog
from ..permissions import HasApiScope

logger = logging.getLogger("sendstack")


class PublicAPIView(APIView):
    """
    Base for every ``/api/public/v1/`` endpoint.

    Subclasses set `required_scopes` and implement the HTTP verbs. Two optional
    hooks exist for the endpoints that need them: `rate_limit_buckets` (which
    buckets this endpoint charges) and `idempotent` (whether it honours
    ``Idempotency-Key``).
    """

    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [HasApiScope]

    #: Overridden by every subclass. `None` is refused by HasApiScope -- see
    #: permissions.py for why that is a bug rather than a public endpoint.
    required_scopes = None

    #: For endpoints where the verb decides the permission: reading a template
    #: needs `templates:read`, writing one needs `templates:write`, and both
    #: live on the same URL.
    #:
    #: Declarative rather than an `initial` override per view, because four
    #: near-identical overrides is four chances to get one wrong -- and because
    #: a scope only ever assigned inside a method is invisible to anything that
    #: wants to check the scope list is fully used (see ScopeCoverageTests).
    #: Any verb not listed falls back to `required_scopes`.
    method_scopes = None

    #: DRF's own throttles are disabled here: this app does its own, keyed on
    #: the API key rather than on the user or the IP, and running both would
    #: mean two independent 429s with two different sets of headers.
    throttle_classes = []

    #: Which rate-limit buckets a request to this view spends. Every endpoint
    #: charges `requests_per_minute`; the send and campaign endpoints add more.
    rate_limit_buckets = ("requests_per_minute",)

    def get_exception_handler(self):
        # Per-view, not the project-wide DRF setting -- see errors.py.
        return public_api_exception_handler

    def initial(self, request, *args, **kwargs):
        """Runs after authentication and permissions, before the handler."""
        self._started_at = time.monotonic()
        self._rate_limit_headers = {}
        self._log_extra = {}

        # Before super(), because that is what runs the permission check.
        if self.method_scopes:
            self.required_scopes = self.method_scopes.get(request.method, self.required_scopes)

        self._require_https(request)
        super().initial(request, *args, **kwargs)

        # After super(), so an unauthenticated request is refused as
        # unauthenticated rather than being rate limited into a confusing 429.
        api_key = request.auth
        if api_key is not None:
            for bucket in self.rate_limit_buckets:
                result = throttling.enforce(api_key, bucket)
                # The last bucket checked wins the headers. The request bucket
                # is listed first everywhere, so an endpoint that also charges
                # messages reports the message ceiling -- which is the one that
                # will stop the caller first and the one they need to pace on.
                self._rate_limit_headers = result.headers()

    def handle_exception(self, exc):
        # A rate-limit refusal carries the headers for the bucket that actually
        # refused it -- see throttling.enforce. Anything else falls back to
        # whatever the request last charged.
        carried = getattr(exc, "rate_limit_headers", None)
        if carried:
            self._rate_limit_headers = carried

        response = super().handle_exception(exc)
        # Attached here too. A 429 with no X-RateLimit-Reset is a 429 a client
        # can only respond to by guessing.
        self._apply_rate_limit_headers(response)
        return response

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        self._apply_rate_limit_headers(response)
        # Correlates a customer's support question with our logs, and is the
        # one piece of internal state it is safe to hand back.
        response["X-Request-Id"] = get_request_id()
        self._record(request, response)
        return response

    # --- helpers ---------------------------------------------------------

    def _apply_rate_limit_headers(self, response):
        for header, value in getattr(self, "_rate_limit_headers", {}).items():
            response[header] = value

    @staticmethod
    def _require_https(request):
        """
        Refuse plaintext HTTP where the deployment says to.

        The API key travels in a header on every single request, so an
        integration that is accidentally configured with an `http://` base URL
        leaks its credential to every hop on the path, repeatedly, and nothing
        in the response tells it so. Refusing is the only signal that reaches
        the developer.
        """
        require = getattr(settings, "PUBLIC_API_REQUIRE_HTTPS", None)
        if require is None:
            require = not settings.DEBUG
        if require and not request.is_secure():
            raise PublicAPIError(
                HTTPS_REQUIRED,
                "The API is only available over HTTPS. Your API key was sent in "
                "cleartext -- rotate it before continuing.",
                status_code=403,
            )

    def _record(self, request, response):
        """
        Write the access-log row and stamp the key.

        Best-effort in exactly the same sense as `apps.audit.services.log_event`:
        the work being logged has already happened, so a failure here must not
        turn a successful send into a 500.
        """
        api_key = getattr(request, "auth", None)
        if api_key is None:
            return

        try:
            duration_ms = int((time.monotonic() - getattr(self, "_started_at", time.monotonic())) * 1000)
            error_code = ""
            data = getattr(response, "data", None)
            if isinstance(data, dict) and isinstance(data.get("error"), dict):
                error_code = (data["error"].get("code") or "")[:40]

            forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
            ip = forwarded_for.split(",")[0].strip() or request.META.get("REMOTE_ADDR") or None

            ApiRequestLog.objects.create(
                tenant_id=api_key.tenant_id,
                api_key=api_key,
                key_prefix=api_key.key_prefix,
                method=request.method,
                # Truncated rather than dropped: the path is how somebody finds
                # the call they are looking for, and a 255-char path is a bug
                # in the caller, not something to preserve faithfully.
                path=request.path[:255],
                status_code=response.status_code,
                error_code=error_code,
                ip_address=ip,
                user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:255],
                request_id=get_request_id()[:64],
                duration_ms=duration_ms,
                message_id=self._log_extra.get("message_id"),
                campaign_id=self._log_extra.get("campaign_id"),
            )
            record_usage(api_key, request, messages=self._log_extra.get("messages_sent", 0))
        except Exception:  # noqa: BLE001 -- see the docstring
            logger.exception("Could not write the public API request log.")

    # --- response helpers ------------------------------------------------

    @staticmethod
    def ok(data=None, *, status_code=200, **extra):
        """
        The success envelope. `success: true` on every 2xx, for the same reason
        `success: false` is on every failure: a client should be able to branch
        on one field without first knowing which endpoint it called.
        """
        body = {"success": True}
        if isinstance(data, dict):
            body.update(data)
        elif data is not None:
            body["data"] = data
        body.update(extra)
        return Response(body, status=status_code)

    @staticmethod
    def fail(code, message=None, *, status_code=400, extra=None):
        return error_response(code, message, status_code=status_code, extra=extra)

    def paginate(self, queryset, request, serialize, *, default_limit=50, max_limit=200):
        """
        Cursor-free limit/offset paging.

        Deliberately not DRF's paginator: this API publishes its own response
        shape, and the internal one wraps results in `count`/`next`/`previous`
        with absolute URLs built from the internal host. Simple offsets are also
        what an integration author expects to be able to loop over without
        parsing a URL.
        """
        try:
            limit = int(request.query_params.get("limit", default_limit))
        except (TypeError, ValueError):
            limit = default_limit
        try:
            offset = int(request.query_params.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0

        limit = max(1, min(limit, max_limit))
        offset = max(0, offset)

        total = queryset.count()
        page = list(queryset[offset:offset + limit])
        return {
            "data": [serialize(item) for item in page],
            "pagination": {
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(page) < total,
            },
        }


class IdempotentMixin:
    """
    Adds ``Idempotency-Key`` support to a POST endpoint.

    Wraps the handler rather than the whole view: only the endpoints that
    actually cause something irreversible need this, and applying it to reads
    would store responses nobody will ever replay.
    """

    def run_idempotent(self, request, endpoint: str, handler):
        from .. import idempotency

        record, replay = idempotency.claim(request, request.auth, endpoint, request.data)
        if replay is not None:
            response = Response(replay["body"], status=replay["status_code"])
            # So a client can tell a replay from a fresh execution -- which is
            # the difference between "my retry worked" and "I sent it twice".
            response["Idempotency-Replayed"] = "true"
            return response

        try:
            response = handler()
        except Exception:
            # The request failed and sent nothing, so the key must be usable
            # again -- see idempotency.abandon.
            idempotency.abandon(record)
            raise

        idempotency.complete(record, response.status_code, response.data)
        return response
