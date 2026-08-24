from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.audit.models import AuditEvent
from apps.credits.services import CreditError, reclaim_credits_on_delete
from apps.tenants.models import Tenant
from core.bulk import BulkActionError, BulkActionMixin

from .serializers import (
    CurrentUserSerializer,
    MultiSenderTokenSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    ManagedUserCreateSerializer,
    ManagedUserSerializer,
    ImpersonationStartSerializer,
)
from .models import ImpersonationSession


class LoginView(TokenObtainPairView):
    serializer_class = MultiSenderTokenSerializer

    def post(self, request, *args, **kwargs):
        # Deliberately not calling super().post(): TokenObtainPairView.post()
        # authenticates the user internally (one query) but doesn't expose
        # the serializer it built, so the previous version of this method had
        # to look the user up a second time by username just to log the
        # audit event -- a duplicate query on every single login. Inlining
        # TokenViewBase.post()'s logic here (identical response/error
        # behavior) lets us reuse `serializer.user` instead.
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as exc:
            raise InvalidToken(exc.args[0]) from exc

        user = serializer.user
        if user.tenant_id:
            AuditEvent.objects.create(actor=user, tenant_id=user.tenant_id, action="auth.login")

        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"data": CurrentUserSerializer(request.user).data})


class PasswordResetRequestView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = get_user_model().objects.filter(
            email__iexact=serializer.validated_data["email"], is_active=True
        ).select_related("tenant").first()

        # Always return the same response so this endpoint cannot enumerate users.
        if user:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            reset_url = f"{settings.FRONTEND_URL}/reset-password?uid={uid}&token={token}"
            send_mail(
                subject="Reset your MultiSender password",
                message=f"Use this link to set a new password: {reset_url}",
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=False,
            )
            if user.tenant_id:
                AuditEvent.objects.create(actor=user, tenant=user.tenant, action="auth.password_reset_requested")
        return Response({"detail": "If an active account uses this email, a reset link has been sent."})


class PasswordResetConfirmView(APIView):
    authentication_classes = []
    permission_classes = []

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
        if user.tenant_id:
            AuditEvent.objects.create(actor=user, tenant=user.tenant, action="auth.password_reset_completed")
        return Response({"detail": "Password updated successfully."})


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
