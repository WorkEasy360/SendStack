import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import status, viewsets
from rest_framework.exceptions import Throttled
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.audit.models import AuditEvent
from apps.credits.services import CreditError, reclaim_credits_on_delete
from apps.subscriptions.entitlements import EntitlementError
from apps.subscriptions.services import quota_guard
from apps.tenants.models import Tenant
from core import lockout
from core.bulk import BulkActionError, BulkActionMixin

from .serializers import (
    CurrentUserSerializer,
    SendStackTokenSerializer,
    PasswordChangeSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    ManagedUserCreateSerializer,
    ManagedUserSerializer,
    ImpersonationStartSerializer,
)
from .models import ImpersonationSession
from . import emails
from .sessions import revoke_all_refresh_tokens

logger = logging.getLogger("sendstack")


#: Scope for every endpoint where the request *is* a guess at a credential.
#: Named "auth" to match settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],
#: which has always defined a 10/min rate for it -- it simply had nothing
#: using it, so login fell back to the far looser anonymous rate.
AUTH_THROTTLE_SCOPE = "auth"

#: Scope name for core.lockout's per-account counter on login.
LOGIN_LOCKOUT_SCOPE = "login"

#: Scope for the per-*email* counter on password-reset requests. Separate from
#: the login scope on purpose: a mailbox being flooded and an account being
#: guessed at are different problems with different thresholds, and spending
#: one budget must not lock the other.
PASSWORD_RESET_SCOPE = "password_reset"


class LoginView(TokenObtainPairView):
    serializer_class = SendStackTokenSerializer

    # ScopedRateThrottle rather than the default anon/user pair: this endpoint
    # is unauthenticated, so DEFAULT_THROTTLE_CLASSES only ever applied
    # AnonRateThrottle's 60/min to it -- six times the rate the deployment
    # thought it had configured for authentication.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = AUTH_THROTTLE_SCOPE

    def post(self, request, *args, **kwargs):
        # Deliberately not calling super().post(): TokenObtainPairView.post()
        # authenticates the user internally (one query) but doesn't expose
        # the serializer it built, so the previous version of this method had
        # to look the user up a second time by username just to log the
        # audit event -- a duplicate query on every single login. Inlining
        # TokenViewBase.post()'s logic here (identical response/error
        # behavior) lets us reuse `serializer.user` instead.
        #
        # The rate throttle above counts requests per client address, which an
        # attacker rotating addresses walks straight past. The lockout counts
        # attempts against the *account*, which they cannot.
        identifier = str(request.data.get("username") or "")

        if lockout.is_locked(LOGIN_LOCKOUT_SCOPE, identifier):
            # 429 with no wait hint, and the same response whether or not this
            # username exists -- see the note on record_failure below.
            raise Throttled(detail="Too many failed sign-in attempts. Try again later.")

        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as exc:
            # Counted too: a malformed-token error here is still a failed
            # attempt against this identifier.
            lockout.record_failure(LOGIN_LOCKOUT_SCOPE, identifier)
            raise InvalidToken(exc.args[0]) from exc
        except Exception:
            # Every other failure -- wrong password, unknown username, inactive
            # account, a missing field -- counts, and counts identically.
            # Counting only *real* accounts would turn the lockout into an
            # existence oracle: locking on the eleventh attempt would mean
            # "this account is real".
            #
            # Deliberately `Exception` and not the two or three types SimpleJWT
            # happens to raise today. Over-counting is the safe direction for a
            # brute-force control -- the worst case is an account briefly
            # locked during an outage it could not have logged in through
            # anyway -- whereas naming the types means a library change quietly
            # stops the counter and nothing fails to tell us.
            lockout.record_failure(LOGIN_LOCKOUT_SCOPE, identifier)
            raise

        user = serializer.user

        # Checked here rather than inside the serializer, and only *after* the
        # credentials have been accepted. Inside `validate` it would surface as
        # a validation error, which the `except Exception` above counts as a
        # failed attempt -- so a suspended workspace would spend its users'
        # lockout budget and eventually lock accounts that did nothing wrong.
        #
        # Told plainly, because somebody who signs in successfully and is then
        # refused by every screen has a much worse time than somebody who is
        # told what happened. 401: it is an authentication refusal, and the
        # frontend already routes those to the sign-in screen.
        tenant = getattr(user, "tenant", None)
        if tenant is not None and not tenant.is_active:
            lockout.clear(LOGIN_LOCKOUT_SCOPE, identifier)
            return Response(
                {"detail": "This workspace is suspended. Contact support."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        lockout.clear(LOGIN_LOCKOUT_SCOPE, identifier)
        if user.tenant_id:
            AuditEvent.objects.create(actor=user, tenant_id=user.tenant_id, action="auth.login")

        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class LogoutView(APIView):
    """
    End a session on the server, not just in the browser.

    Clearing localStorage removes the tokens from one device and does nothing
    to the tokens themselves: a refresh token copied off that machine stayed
    valid for its full seven days, so "log out" on a shared or lost computer
    protected nobody. This blacklists the refresh token that is presented, at
    which point it -- and every access token later minted from it -- is dead.

    Unauthenticated on purpose. The refresh token *is* the credential being
    surrendered, and the access token that accompanies it has a 15-minute life,
    so requiring a live one would mean the most important case (a session left
    open long enough to matter) is the one that could not be ended.

    Always answers 200. Logging out is not an operation a caller can usefully
    retry, an already-dead token is the outcome they asked for, and a
    distinguishable error would say whether a given token string was ever real.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = AUTH_THROTTLE_SCOPE

    def post(self, request):
        raw = request.data.get("refresh")
        if raw:
            try:
                RefreshToken(raw).blacklist()
            except TokenError:
                # Expired, already blacklisted, or never valid. All three mean
                # the same thing to the caller: it cannot be used again.
                pass
        return Response({"detail": "Signed out."}, status=status.HTTP_200_OK)


class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"data": CurrentUserSerializer(request.user).data})


#: The one sentence this endpoint ever says. A constant because it must be
#: byte-identical for an address that exists and one that does not -- two
#: wordings that differ by so much as a full stop are an enumeration oracle.
RESET_REQUESTED_DETAIL = (
    "If an account exists for this email, a password reset link has been sent."
)


class PasswordResetRequestView(APIView):
    authentication_classes = []
    permission_classes = []
    # Same scope as login. This endpoint sends real email to a real address on
    # request, so an unthrottled one is both an enumeration probe and a way to
    # have this deployment mail-bomb somebody.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = AUTH_THROTTLE_SCOPE

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        # Two throttles on two different axes, because they stop two different
        # attacks. The DRF scope above counts per client address and stops one
        # machine hammering the endpoint. This counts per *mailbox*, and is the
        # only one that stops somebody with a proxy pool filling one person's
        # inbox -- every one of those requests comes from a different address
        # and none of them trips a per-client rate.
        #
        # Counted before the user lookup, and the response is the same either
        # way, so the throttle itself cannot be used to test whether an address
        # is registered.
        over_limit = _password_reset_over_limit(email)

        user = get_user_model().objects.filter(
            email__iexact=email, is_active=True
        ).select_related("tenant").first()

        if user and not over_limit:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            reset_url = f"{settings.FRONTEND_URL}/reset-password?uid={uid}&token={token}"
            # Never logged, never echoed, and not passed through any
            # intermediate that records its arguments: the token in this URL is
            # a live credential for the next hour.
            emails.send_password_reset(
                user, reset_url, expires_in_hours=_reset_expiry_hours(),
            )
            if user.tenant_id:
                AuditEvent.objects.create(
                    actor=user, tenant=user.tenant, action="auth.password_reset_requested",
                )
        elif over_limit:
            logger.warning(
                "Password-reset requests for one address are over the limit; not sending."
            )

        return Response({"detail": RESET_REQUESTED_DETAIL})


def _reset_expiry_hours() -> int:
    seconds = int(getattr(settings, "PASSWORD_RESET_TIMEOUT", 3600))
    return max(seconds // 3600, 1)


def _password_reset_over_limit(email: str) -> bool:
    """
    Whether this address has already been sent as many reset mails as it may be.

    Uses core.lockout's hashed-key counter, so the addresses currently being
    targeted are not sitting in Redis in the clear. Its own limits, not the
    login ones: asking twice because the first mail was slow is normal, and
    being locked out of password recovery for fifteen minutes after two
    attempts would turn a safeguard into a support ticket.

    Fails **open**, like everything built on that cache: if Redis is down,
    somebody who has genuinely forgotten their password must still be able to
    recover, and the per-address DRF throttle still applies.
    """
    limit = int(getattr(settings, "PASSWORD_RESET_MAX_PER_EMAIL", 5))
    if limit <= 0:
        return False

    key = lockout.cache_key(PASSWORD_RESET_SCOPE, email)
    window = int(getattr(settings, "PASSWORD_RESET_EMAIL_WINDOW_SECONDS", 3600))
    if cache.add(key, 1, timeout=window):
        return False
    try:
        return int(cache.incr(key)) > limit
    except ValueError:
        cache.add(key, 1, timeout=window)
        return False


class PasswordResetConfirmView(APIView):
    authentication_classes = []
    permission_classes = []
    # A reset token is a secret being guessed at, exactly like a password.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = AUTH_THROTTLE_SCOPE

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user_id = force_str(urlsafe_base64_decode(serializer.validated_data["uid"]))
            user = get_user_model().objects.select_related("tenant").get(pk=user_id, is_active=True)
        except (TypeError, ValueError, OverflowError, get_user_model().DoesNotExist):
            user = None
        if not user or not default_token_generator.check_token(user, serializer.validated_data["token"]):
            return Response({"detail": "This password-reset link is invalid or has expired."}, status=400)

        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])

        # The token dies here, and it dies for free. Django's
        # default_token_generator hashes the user's current password hash into
        # the token, so changing the password makes every token minted before
        # this line fail check_token -- single use, and no token table to keep.
        # The same property is what makes an old link unusable after a *later*
        # reset, which is the reuse case the audit asked about.

        # Every session this account had is now dead. Without this, a refresh
        # token stolen before the reset keeps minting 15-minute access tokens
        # for its full seven-day life -- so "I reset my password because I
        # think somebody has my account" would have accomplished nothing at
        # all. Access tokens already issued still work until they expire;
        # that 15-minute window is the honest remaining exposure, and closing
        # it would mean a blacklist lookup on every API call.
        revoke_all_refresh_tokens(user)

        # Proving control of the mailbox is a stronger signal than the failed
        # attempts that may have locked this account, so the counter goes.
        # Without this, someone who forgot their password, guessed until they
        # were locked out and then reset it would still be refused.
        lockout.clear(LOGIN_LOCKOUT_SCOPE, user.get_username())
        if user.tenant_id:
            AuditEvent.objects.create(actor=user, tenant=user.tenant, action="auth.password_reset_completed")

        # Sent last and never allowed to fail the request: the password is
        # already changed, and refusing to say so because the mail server is
        # down would leave somebody unable to sign in with either password.
        emails.send_password_changed(user, reason="reset")

        return Response({"detail": "Password updated successfully."})


class PasswordChangeView(APIView):
    """
    Change your own password, while signed in.

    Requires the current password even though the caller is already
    authenticated. That is not ceremony: an access token found on an unlocked
    laptop is enough to reach this endpoint, and without the old password it
    would be enough to take the account permanently. Knowing the current
    password is the one thing the person holding a stolen token does not.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = AUTH_THROTTLE_SCOPE

    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data, context={"user": request.user})
        serializer.is_valid(raise_exception=True)

        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])

        # Same reasoning as the reset path: the point of changing a password is
        # that the old credential stops working everywhere, not just here.
        revoke_all_refresh_tokens(user)
        lockout.clear(LOGIN_LOCKOUT_SCOPE, user.get_username())
        if user.tenant_id:
            AuditEvent.objects.create(actor=user, tenant=user.tenant, action="auth.password_changed")
        emails.send_password_changed(user, reason="changed")

        return Response({
            "detail": "Password updated. You have been signed out on your other devices.",
        })


class UserActionError(Exception):
    """A user-management rule refused this account, with the status to report."""

    def __init__(self, detail, status_code):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class ManagedUserViewSet(BulkActionMixin, viewsets.ModelViewSet):
    """Hierarchy-scoped user administration; never exposes password hashes."""

    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    bulk_object_label = "account"
    bulk_actions = {"delete": "_bulk_delete", "activate": "_bulk_activate", "suspend": "_bulk_suspend"}

    def get_queryset(self):
        user_model = get_user_model()
        actor = self.request.user
        queryset = user_model.objects.select_related("tenant", "tenant__parent").filter(is_deleted=False)
        if actor.role == actor.Role.SUPERADMIN:
            return queryset
        if actor.role == actor.Role.RESELLER and actor.tenant_id:
            return queryset.filter(role=actor.Role.USER, tenant__parent_id=actor.tenant_id)
        return queryset.filter(pk=actor.pk)

    def get_serializer_class(self):
        return ManagedUserCreateSerializer if self.action == "create" else ManagedUserSerializer

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        actor = request.user
        requested_role = serializer.validated_data["role"]

        if actor.role == actor.Role.RESELLER:
            if requested_role != actor.Role.USER or not actor.tenant_id:
                return Response({"detail": "Resellers may create users only."}, status=status.HTTP_403_FORBIDDEN)
            parent = actor.tenant
            tenant_kind = Tenant.Kind.CUSTOMER
        elif actor.role == actor.Role.SUPERADMIN:
            if not actor.tenant_id:
                return Response({"detail": "Your account has no tenant."}, status=status.HTTP_400_BAD_REQUEST)
            parent = actor.tenant
            tenant_kind = Tenant.Kind.RESELLER if requested_role == actor.Role.RESELLER else Tenant.Kind.CUSTOMER
        else:
            return Response({"detail": "You cannot create users."}, status=status.HTTP_403_FORBIDDEN)

        tenant = Tenant.objects.create(
            name=serializer.validated_data["tenant_name"], kind=tenant_kind, parent=parent
        )
        # `max_users` counts the active, undeleted users **of the tenant the
        # new account will belong to** -- not the actor's tenant, not the
        # tenants below it, and never a superadmin or a reseller from
        # somewhere else in the hierarchy. See docs/ENTITLEMENTS.md.
        #
        # Today that tenant is always the one created on the line above, so
        # the count is zero and the ceiling never bites: this endpoint gives
        # every account its own workspace, which is the reason the audit found
        # the team subsystem unreachable. The check is here rather than
        # waiting for that to change, because the moment a second user can
        # join an existing workspace this is the line that has to stop them,
        # and adding it later means shipping the feature unenforced first.
        with quota_guard(tenant.pk, "users"):
            new_user = get_user_model().objects.create_user(
                username=serializer.validated_data["username"],
                email=serializer.validated_data["email"],
                password=serializer.validated_data["password"],
                mobile=serializer.validated_data.get("mobile", ""),
                role=requested_role,
                tenant=tenant,
            )
        AuditEvent.objects.create(
            actor=actor, tenant=tenant, action="users.created", object_type="accounts.User", object_id=str(new_user.pk),
            metadata={"role": new_user.role},
        )
        return Response(ManagedUserSerializer(new_user).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        target = self.get_object()
        if target.pk != request.user.pk and not request.user.can_manage(target):
            return Response({"detail": "You cannot modify this user."}, status=status.HTTP_403_FORBIDDEN)
        serializer = self.get_serializer(target, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        AuditEvent.objects.create(
            actor=request.user, tenant=target.tenant, action="users.updated", object_type="accounts.User", object_id=str(target.pk)
        )
        return Response(serializer.data)

    def _delete_user(self, request, target):
        """The soft-delete + credit reclaim, shared by DELETE and bulk delete."""
        actor = request.user
        if target.pk == actor.pk or not actor.can_manage(target):
            raise UserActionError("You cannot delete this user.", status.HTTP_403_FORBIDDEN)

        try:
            reclaimed = reclaim_credits_on_delete(target=target, actor=actor)
        except CreditError as exc:
            raise UserActionError(str(exc), status.HTTP_400_BAD_REQUEST) from exc

        target.is_active = False
        target.is_deleted = True
        target.deleted_at = timezone.now()
        target.save(update_fields=["is_active", "is_deleted", "deleted_at"])
        AuditEvent.objects.create(
            actor=actor, tenant=target.tenant, action="users.deleted", object_type="accounts.User",
            object_id=str(target.pk), metadata={"credits_reclaimed": reclaimed or 0},
        )

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        try:
            self._delete_user(request, self.get_object())
        except UserActionError as exc:
            return Response({"detail": exc.detail}, status=exc.status_code)
        return Response(status=status.HTTP_204_NO_CONTENT)

    # Bulk handlers run the same rules one account at a time, so credits are
    # reclaimed per account and each refusal is reported against the account
    # that caused it. Each delete gets its own transaction: one account whose
    # credits cannot be reclaimed must not roll back the others.
    def _bulk_delete(self, request, instance):
        try:
            with transaction.atomic():
                self._delete_user(request, instance)
        except UserActionError as exc:
            raise BulkActionError(exc.detail) from exc

    def _set_active(self, request, target, is_active):
        # Deliberately stricter than PATCH, which lets you edit yourself: a
        # bulk suspend that could lock the actor out of their own console is
        # not a mistake worth allowing.
        if target.pk == request.user.pk:
            raise BulkActionError("You cannot change your own account here.")
        # Not can_manage(): that returns False for an already-suspended target,
        # which would make "activate" impossible -- the one thing a suspended
        # account is selected for. The hierarchy is already enforced, because
        # bulk only ever sees rows from this viewset's get_queryset().
        if request.user.role not in (request.user.Role.SUPERADMIN, request.user.Role.RESELLER):
            raise BulkActionError("You cannot modify this user.")
        if target.is_active == is_active:
            return
        if is_active:
            # Reactivating puts somebody back into their workspace's headcount,
            # so it is a create as far as the plan is concerned. Suspending
            # never is -- an account can always be taken *out* of a full plan.
            try:
                with quota_guard(target.tenant_id, "users"):
                    target.is_active = True
                    target.save(update_fields=["is_active"])
            except EntitlementError as exc:
                raise BulkActionError(str(exc.detail)) from exc
        else:
            target.is_active = is_active
            target.save(update_fields=["is_active"])
        AuditEvent.objects.create(
            actor=request.user, tenant=target.tenant, action="users.updated",
            object_type="accounts.User", object_id=str(target.pk), metadata={"is_active": is_active},
        )

    def _bulk_activate(self, request, instance):
        self._set_active(request, instance, True)

    def _bulk_suspend(self, request, instance):
        self._set_active(request, instance, False)


class ImpersonationStartView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        serializer = ImpersonationStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target = get_user_model().objects.select_related("tenant", "tenant__parent").filter(
            pk=serializer.validated_data["user_id"], is_active=True
        ).first()
        if not target or not request.user.can_manage(target):
            return Response({"detail": "You cannot impersonate this user."}, status=status.HTTP_403_FORBIDDEN)

        refresh = RefreshToken.for_user(target)
        expires_at = timezone.now() + timedelta(hours=1)
        forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
        session = ImpersonationSession.objects.create(
            original_user=request.user,
            impersonated_user=target,
            expires_at=expires_at,
            reason=serializer.validated_data["reason"],
            ip_address=forwarded_for.split(",")[0].strip() or request.META.get("REMOTE_ADDR") or None,
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
            refresh_jti=str(refresh["jti"]),
        )
        access = refresh.access_token
        for token in (refresh, access):
            token["role"] = target.role
            token["tenant_id"] = target.tenant_id
            token["original_user_id"] = request.user.pk
            token["impersonation_session_id"] = session.pk
        AuditEvent.objects.create(
            actor=request.user, acting_as=target, tenant=target.tenant, action="impersonation.started",
            object_type="accounts.User", object_id=str(target.pk), metadata={"reason": session.reason},
        )
        return Response({
            "access": str(access), "refresh": str(refresh),
            "impersonation": {"session_id": session.pk, "original_user": request.user.username, "acting_as": ManagedUserSerializer(target).data, "expires_at": expires_at},
        }, status=status.HTTP_201_CREATED)


class ImpersonationExitView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = request.auth
        session_id = token.get("impersonation_session_id") if token else None
        original_user_id = token.get("original_user_id") if token else None
        session = ImpersonationSession.objects.select_related("original_user", "impersonated_user", "impersonated_user__tenant").filter(
            pk=session_id, original_user_id=original_user_id, impersonated_user=request.user, ended_at__isnull=True
        ).first()
        if not session:
            return Response({"detail": "No active impersonation session was found."}, status=status.HTTP_400_BAD_REQUEST)
        session.ended_at = timezone.now()
        session.save(update_fields=["ended_at"])
        AuditEvent.objects.create(
            actor=session.original_user, acting_as=session.impersonated_user, tenant=session.impersonated_user.tenant,
            action="impersonation.ended", object_type="accounts.User", object_id=str(session.impersonated_user_id),
        )
        return Response({"detail": "Impersonation session ended."})
