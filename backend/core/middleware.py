import logging
import uuid

from django.conf import settings
from django.http import Http404, HttpResponse

from . import lockout
from .request_context import reset_request_id, set_request_id

REQUEST_ID_HEADER = "X-Request-ID"

logger = logging.getLogger("sendstack")


class RequestIDMiddleware:
    """Attach a correlation id to every request, response, and log line."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.request_id = request_id
        token = set_request_id(request_id)
        try:
            response = self.get_response(request)
        finally:
            reset_request_id(token)
        response[REQUEST_ID_HEADER] = request_id
        return response


def client_ip(request) -> str:
    """
    The address this request came from, trusting the proxy in front of us.

    nginx sets X-Forwarded-For and is the only thing that can reach gunicorn
    (the backend container publishes no port), so its left-most entry is the
    real client. Where there is no proxy the header is absent and REMOTE_ADDR
    is already correct.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",")[0].strip() or request.META.get("REMOTE_ADDR") or "").strip()


class AdminSecurityMiddleware:
    """
    Keeps /admin/ from being a public password-guessing endpoint.

    The Django admin is a full-privilege login form with no throttling of its
    own: DRF's throttles never see it, because it is not a DRF view. Left as
    shipped it will answer an unlimited number of password attempts from an
    unlimited number of addresses, which for an interface that can read every
    tenant's data is the sharpest edge in the deployment.

    Two controls, in order:

    * An optional address allowlist (``ADMIN_ALLOWED_IPS``). Off by default,
      because an operator without a fixed address would lock themselves out of
      their own admin; where it *is* set, a request from anywhere else gets a
      404, not a 403 -- there is no reason to confirm that an admin exists here.

    * A per-username failed-attempt lockout on the login POST, sharing
      core.lockout with the API's own login. Both count into the same scope, so
      spending attempts on /admin/ also locks the account out of the API and
      vice versa -- an attacker cannot use the two forms as two budgets.

    nginx rate-limits this path as well (see nginx/nginx.conf), which stops the
    requests before a worker is spent on them. This is the half that survives a
    deployment where nginx is replaced by something else.
    """

    #: Distinct from the API's login scope only in that a hit here is recorded
    #: against the same account, so the two forms share one budget.
    LOCKOUT_SCOPE = "login"

    def __init__(self, get_response):
        self.get_response = get_response

    def _admin_prefix(self) -> str:
        return "/" + getattr(settings, "DJANGO_ADMIN_URL", "admin/").lstrip("/")

    def __call__(self, request):
        if not request.path.startswith(self._admin_prefix()):
            return self.get_response(request)

        allowed = getattr(settings, "ADMIN_ALLOWED_IPS", None) or []
        if allowed and client_ip(request) not in allowed:
            logger.warning("Admin access refused for an address outside ADMIN_ALLOWED_IPS.")
            raise Http404()

        # Only the login form itself is guessable; the rest of the admin is
        # already behind an authenticated session.
        is_login_attempt = request.method == "POST" and "login" in request.path
        identifier = request.POST.get("username", "") if is_login_attempt else ""

        if is_login_attempt and lockout.is_locked(self.LOCKOUT_SCOPE, identifier):
            logger.warning("Admin sign-in refused: too many failed attempts for this account.")
            # Plain text, and the same for a real account as for an invented
            # one, so this cannot be used to test whether a username exists.
            return HttpResponse(
                "Too many failed sign-in attempts. Try again later.",
                status=429,
                content_type="text/plain",
            )

        response = self.get_response(request)

        if is_login_attempt:
            # The admin answers a successful login with a redirect and a failed
            # one by re-rendering the form, which is the only signal available
            # from out here without reaching into the view.
            if response.status_code == 302:
                lockout.clear(self.LOCKOUT_SCOPE, identifier)
            else:
                lockout.record_failure(self.LOCKOUT_SCOPE, identifier)

        return response
