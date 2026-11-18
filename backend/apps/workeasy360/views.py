"""
The one WorkEasy360 endpoint SendStack serves itself, next to the connector's.

``GET session/`` -- what the single-page app needs: is WorkEasy360 sign-in on,
is it the only way in, where to start and end it, and the CSRF token a cookie
session sends back on writes (the CSRF cookie is httpOnly in production, so
the app cannot read it from the cookie).

``start/``, ``callback/``, ``logout/``, ``logout/workeasy360/``,
``backchannel-logout/`` and ``status/`` are the connector's own views (see
urls.py); SendStack's rules run inside them through its adapters
(adapters.py).
"""

from django.http import HttpResponseNotAllowed, JsonResponse
from django.middleware.csrf import get_token
from django.urls import NoReverseMatch, reverse
from django.views.decorators.cache import never_cache

from workeasy360_sso import api
from workeasy360_sso.organizations import get_context

from . import services


def _route(name):
    try:
        return reverse(f"workeasy360_sso:{name}")
    except NoReverseMatch:
        return ""


@never_cache
def session_status(request):
    if request.method not in ("GET", "HEAD"):
        return HttpResponseNotAllowed(["GET"])
    cfg = services.config()
    enabled = bool(cfg is not None and cfg.is_configured)
    end_session = ""
    if enabled:
        try:
            end_session = api.end_session_url(cfg)
        except Exception:  # noqa: BLE001 - discovery unreachable: the option is simply not offered
            end_session = ""
    return JsonResponse({
        "enabled": enabled,
        "sso_only": services.sso_only(),
        "signed_in": get_context(request) is not None,
        "csrf_token": get_token(request),
        "start_url": _route("start") if enabled else "",
        "logout_url": _route("logout"),
        "logout_workeasy360_url": _route("logout_workeasy360") if end_session else "",
        "messages": services.ERROR_MESSAGES,
    })
