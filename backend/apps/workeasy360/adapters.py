"""
SendStack's rules for a WorkEasy360 sign-in, plugged into the connector.

The connector (``workeasy360_sso``) verifies the ID token -- signature by
``kid``, exact issuer, audience, expiry, nonce, ``product_code`` and the
presence of ``organization_id`` -- and links the person on ``(issuer, sub)``.
It then calls these two adapters, in **one database transaction**, so a
refusal here leaves no half-made account or workspace behind:

* :class:`SendStackUserAdapter` -- who the person is in SendStack;
* :class:`SendStackOrganizationAdapter` -- which workspace they land in.

The rules, in one place:

1. ``email_verified`` must be true. The connector checks it only when it
   links by address, which SendStack never does, so it is checked here for
   every sign-in.
2. Never merge on an email address. A new WorkEasy360 person whose address
   already belongs to a SendStack account is refused (``account_conflict``)
   until an operator links the two deliberately (``w360_link_user``).
3. One organization, one workspace. The first sign-in of an organization
   creates its workspace (a customer directly under the platform tenant,
   never under a reseller) unless ``WORKEASY360_PROVISION_TENANTS=false``;
   an operator can link an existing workspace instead
   (``w360_link_organization``).
4. A SendStack account belongs to exactly one workspace, so a person who
   signs in for a second WorkEasy360 organization is refused
   (``other_organization``) -- never moved, never given both.
5. A WorkEasy360 session is an ordinary user's (role ``USER``). Reseller and
   superadmin accounts administer other workspaces, which a session bound to
   one organization must never reach, so they are refused
   (``role_not_allowed``).
6. The organization, and the person, must be entitled when they arrive;
   nothing is created for one that is not.
"""

import logging

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from workeasy360_sso.errors import SignInRefused
from workeasy360_sso.organizations import DefaultOrganizationAdapter
from workeasy360_sso.provisioning import DefaultUserAdapter

from . import services
from .models import OrganizationLink

logger = logging.getLogger("sendstack")

DEFAULT_WORKSPACE_NAME = "WorkEasy360 workspace"


class SendStackUserAdapter(DefaultUserAdapter):
    def resolve(self, request, *, claims):
        if claims.get("email_verified") is not True:
            raise SignInRefused("email_unverified", log_reason="email_not_verified")
        if not str(claims.get("email") or "").strip():
            raise SignInRefused("email_missing")
        return super().resolve(request, claims=claims)

    def is_user_allowed(self, user):
        return bool(getattr(user, "is_active", False)) and not getattr(user, "is_deleted", False)

    def create_user(self, request, claims):
        from apps.accounts.registration import normalize_email

        email = normalize_email(str(claims.get("email") or ""))
        user_model = get_user_model()
        if user_model.objects.filter(email__iexact=email, is_deleted=False).exists():
            # Rule 2: an existing account is never taken over by an address.
            raise SignInRefused("account_conflict", log_reason="email_taken")
        user = super().create_user(request, claims)
        # The connector made it with an unusable password; spell out the rest.
        fields = []
        if user.role != user_model.Role.USER:
            user.role = user_model.Role.USER
            fields.append("role")
        if user.is_staff or user.is_superuser:
            user.is_staff = user.is_superuser = False
            fields += ["is_staff", "is_superuser"]
        if fields:
            user.save(update_fields=fields)
        return user


def _platform_tenant():
    """The tenant every WorkEasy360 workspace hangs under: the platform's, never a reseller's."""
    from apps.accounts.models import PLATFORM_TENANT_SLUG
    from apps.tenants.models import Tenant

    platform = Tenant.objects.filter(kind=Tenant.Kind.PLATFORM).order_by("pk").first()
    if platform is None:
        platform = Tenant.objects.create(
            name="SendStack Platform", slug=PLATFORM_TENANT_SLUG, kind=Tenant.Kind.PLATFORM,
        )
    return platform


def _provision_workspace(organization_id, claims):
    """The organization's first sign-in: a new customer workspace, its link and its trial."""
    from apps.accounts.registration import unique_workspace_identity
    from apps.audit.models import AuditEvent
    from apps.onboarding import services as onboarding
    from apps.subscriptions import trials
    from apps.tenants.models import Tenant

    parent = _platform_tenant()
    label = str(claims.get("organization_name") or "").strip()[:140] or DEFAULT_WORKSPACE_NAME
    name, slug = unique_workspace_identity(label, parent)
    tenant = Tenant.objects.create(name=name, slug=slug, kind=Tenant.Kind.CUSTOMER, parent=parent)
    link = OrganizationLink.objects.create(
        organization_id=organization_id, tenant=tenant,
        origin=OrganizationLink.Origin.FIRST_SIGN_IN, linked_by="first sign-in",
    )
    if trials.trials_enabled():
        try:
            trials.start_trial(tenant)
        except trials.TrialUnavailable:
            logger.exception("Trials are enabled but no trial plan resolved for tenant %s.", tenant.pk)
    onboarding.state_for(tenant, guided=True)
    AuditEvent.objects.create(
        tenant=tenant, action="workeasy360.workspace_created",
        object_type="workeasy360.OrganizationLink", object_id=str(link.pk),
        metadata={"organization_id": str(organization_id)},
    )
    logger.info("WorkEasy360 organization %s got workspace %s on its first sign-in.", organization_id, tenant.pk)
    return link


class SendStackOrganizationAdapter(DefaultOrganizationAdapter):
    def bind(self, request, *, user, organization_id, claims):
        from apps.audit.models import AuditEvent
        from apps.subscriptions.entitlements import EntitlementError
        from apps.subscriptions.services import quota_guard
        from apps.tenants.models import Tenant

        user_model = get_user_model()
        if user.role != user_model.Role.USER or user.is_staff or user.is_superuser:
            raise SignInRefused("role_not_allowed")

        # Entitlement when the person arrives (integration guide section 7),
        # before anything is created for an organization that may not have
        # SendStack at all.
        self._require_entitlement(organization_id, str(claims["sub"]))

        link = self._link(organization_id, claims)
        tenant = Tenant.objects.select_for_update().get(pk=link.tenant_id)
        if not tenant.is_active:
            raise SignInRefused("workspace_suspended")
        if tenant.kind != Tenant.Kind.CUSTOMER:
            raise SignInRefused("refused", log_reason="linked_tenant_not_customer")

        if user.tenant_id is None:
            # A new member of this workspace counts against its plan's users,
            # exactly like an account an administrator adds.
            try:
                with quota_guard(tenant.pk, "users"):
                    user.tenant = tenant
                    user.save(update_fields=["tenant", "updated_at"])
            except EntitlementError as exc:
                raise SignInRefused("seat_limit", log_reason="users_quota") from exc
        elif user.tenant_id != tenant.pk:
            raise SignInRefused("other_organization")

        AuditEvent.objects.create(
            actor=user, tenant=tenant, action="auth.login",
            metadata={"method": "workeasy360", "organization_id": str(organization_id)},
        )
        return tenant

    def _require_entitlement(self, organization_id, subject):
        from workeasy360_sso import entitlements

        if not self.config.enforce_entitlement:
            return
        decision = entitlements.check(self.config, organization_id=organization_id, subject=subject)
        if decision.allowed:
            # Includes "no answer yet and WorkEasy360 could not be asked": an
            # outage is never a denial, and WorkEasy360 checked the entitlement
            # itself before it issued the code being spent.
            return
        if decision.code == entitlements.ACCESS_UNVERIFIED:
            raise SignInRefused("access_unverified", log_reason="client_refused")
        code = {"no_seat": "no_seat", "membership_inactive": "provider_membership_inactive"}.get(
            decision.reason, "not_entitled"
        )
        raise SignInRefused(code, log_reason=f"entitlement_{decision.reason}"[:64])

    def _link(self, organization_id, claims):
        link = OrganizationLink.objects.select_for_update().filter(organization_id=organization_id).first()
        if link is not None:
            return link
        if not services.provision_tenants():
            raise SignInRefused("no_workspace")
        try:
            with transaction.atomic():
                return _provision_workspace(organization_id, claims)
        except IntegrityError:
            # Two first sign-ins of one organization raced; the other one made it.
            link = OrganizationLink.objects.select_for_update().filter(organization_id=organization_id).first()
            if link is None:
                raise
            return link
