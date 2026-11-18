"""
How the API recognises a WorkEasy360 sign-in, on every request.

A WorkEasy360 sign-in does **not** produce SimpleJWT tokens and nothing about
it is kept in the browser's localStorage. The callback opens a server-side
Django session (database engine, httpOnly cookie, host-only, Secure in
production), and this class is how DRF accepts that session:

* the session must still be the WorkEasy360 sign-in that opened it: its
  ``ProductSession`` is open, is this session's, and belongs to this person
  under the configured issuer. A back-channel logout deletes the session row
  and closes the ``ProductSession`` in the shared database, so the very next
  request to **any** instance is refused (401);
* the person is still an ordinary user of the one workspace their
  organization is linked to, and that workspace is active;
* CSRF is enforced on unsafe methods (it is a cookie);
* the organization (and the person) are still entitled, asked of WorkEasy360
  on its advertised schedule -- never per request -- by
  :func:`enforce_entitlement`.

A Django session that WorkEasy360 did not open (the Django admin's) is not
an API credential, exactly as before.
"""

from django.contrib.auth import get_user_model
from rest_framework import exceptions, status
from rest_framework.authentication import SessionAuthentication

from . import services

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: Paths a signed-in person can always reach, so the app can say why access
#: ended and offer a way out instead of looking broken.
GATE_EXEMPT_PATHS = (
    "/api/v1/auth/me/",
    "/api/v1/auth/workeasy360/",
)

ACCESS_MESSAGES = {
    "not_entitled": "Your organization does not have SendStack in WorkEasy360.",
    "expired": "Your organization's SendStack subscription in WorkEasy360 has ended.",
    "suspended": "Your organization's access to SendStack is suspended in WorkEasy360.",
    "membership_inactive": "Your access to this organization has ended in WorkEasy360.",
    "no_seat": "You do not have a SendStack seat in WorkEasy360. Ask an administrator for one.",
    "read_only": "Your organization can view SendStack but not make changes until its subscription is renewed.",
    "client_refused": "SendStack cannot confirm your access with WorkEasy360 right now. Try again in a few minutes.",
}


class WorkEasy360Refusal(exceptions.APIException):
    """A refusal with a stable ``code`` the frontend can branch on (merged into the body)."""

    status_code = status.HTTP_403_FORBIDDEN

    def __init__(self, detail, *, code, status_code=None, **extra):
        super().__init__(detail)
        if status_code is not None:
            self.status_code = status_code
        self.extra = {"code": code, **extra}


class WorkEasy360AuthenticationFailed(exceptions.AuthenticationFailed):
    """401, with a stable ``code``."""

    def __init__(self, detail, *, code):
        super().__init__(detail)
        self.extra = {"code": code}


def sign_in_required():
    return WorkEasy360AuthenticationFailed(
        "Sign in with WorkEasy360 to continue.", code="workeasy360_sign_in_required",
    )


class WorkEasy360Auth:
    """``request.auth`` for a WorkEasy360 session: the verified sign-in, never a token."""

    def __init__(self, context, decision=None):
        self.context = context
        self.organization_id = context.organization_id
        self.subject = context.subject
        self.decision = decision

    def get(self, key, default=None):
        # Views written for SimpleJWT read claims with ``request.auth.get(...)``;
        # a WorkEasy360 session has none of them (no impersonation, ever).
        return default


def is_workeasy360_request(request) -> bool:
    return isinstance(getattr(request, "auth", None), WorkEasy360Auth)


def access_summary(request):
    """What ``GET /api/v1/auth/me/`` says about WorkEasy360, or None when it has no say."""
    session = is_workeasy360_request(request)
    decision = getattr(request._request, "workeasy360_access", None)
    if not session and decision is None:
        return None
    summary = {
        "session": session,
        "organization_id": str(request.auth.organization_id) if session else None,
        "access": None,
    }
    if decision is not None:
        summary["access"] = {
            "allowed": decision.allowed,
            "write_allowed": decision.write_allowed,
            "code": decision.code,
            "reason": decision.reason,
            "message": ACCESS_MESSAGES.get(decision.reason, "") if not decision.allowed else "",
        }
    return summary


def enforce_entitlement(request, *, organization_id, subject):
    """WorkEasy360's answer for this organization (and person), enforced.

    The connector caches each answer for ``revalidate_after`` (shared through
    the Django cache, which is Redis in every deployment), re-asks a denial
    after 60 s, never turns an outage into a denial and fails **closed** for
    60 s when WorkEasy360 refuses this product's own client. Returns the
    decision; raises 403 when it forbids this request.
    """
    from workeasy360_sso import entitlements

    cfg = services.config()
    if cfg is None or not cfg.is_configured or not cfg.enforce_entitlement:
        return None
    decision = entitlements.check(cfg, organization_id=organization_id, subject=subject)
    request._request.workeasy360_access = decision
    path = request._request.path_info
    if any(path.startswith(prefix) for prefix in GATE_EXEMPT_PATHS):
        return decision
    if not decision.allowed:
        raise WorkEasy360Refusal(
            ACCESS_MESSAGES.get(decision.reason, ACCESS_MESSAGES["not_entitled"]),
            code=decision.code or entitlements.ACCESS_ENDED, reason=decision.reason,
        )
    if request.method not in SAFE_METHODS and not decision.write_allowed:
        raise WorkEasy360Refusal(ACCESS_MESSAGES["read_only"], code=entitlements.READ_ONLY, reason="read_only")
    return decision


class WorkEasy360SessionAuthentication(SessionAuthentication):
    def authenticate(self, request):
        from workeasy360_sso.models import FederatedIdentity, ProductSession
        from workeasy360_sso.organizations import get_context

        django_request = request._request
        user = getattr(django_request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        context = get_context(django_request)
        if context is None:
            # The Django admin's session, say: never an API credential.
            return None

        cfg = services.config()
        session = django_request.session
        if cfg is None or not cfg.is_configured or context.product_session_id is None:
            session.flush()
            raise sign_in_required()
        live = ProductSession.objects.filter(
            pk=context.product_session_id,
            ended_at__isnull=True,
            session_key=session.session_key,
            identity__user_id=user.pk,
            identity__issuer=cfg.issuer,
            identity__subject=context.subject,
            identity__status=FederatedIdentity.Status.ACTIVE,
        ).exists()
        if not live:
            session.flush()
            raise WorkEasy360AuthenticationFailed(
                "Your WorkEasy360 session has ended. Sign in again.", code="workeasy360_session_ended",
            )

        user_model = get_user_model()
        link = services.link_for_tenant(user.tenant_id)
        if (
            link is None
            or link.organization_id != context.organization_id
            or user.role != user_model.Role.USER
            or getattr(user, "is_deleted", False)
        ):
            session.flush()
            raise WorkEasy360AuthenticationFailed(
                "This session no longer matches your workspace. Sign in again.",
                code="workeasy360_session_ended",
            )
        if not link.tenant.is_active:
            raise exceptions.AuthenticationFailed("This workspace is suspended. Contact support.")

        # A cookie is sent by the browser on its own: unsafe methods must prove
        # they came from SendStack's own pages.
        self.enforce_csrf(request)
        decision = enforce_entitlement(request, organization_id=context.organization_id, subject=context.subject)
        return (user, WorkEasy360Auth(context, decision))
