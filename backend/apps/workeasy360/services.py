"""
The questions the rest of SendStack asks about WorkEasy360 sign-in.

Kept small and import-light on purpose: authentication runs these on every
request, and the accounts, credits and tenants apps call them without having
to know anything about the connector.
"""

import logging
import os
import uuid

from django.db import transaction

from .messages import ERROR_MESSAGES  # noqa: F401 - re-exported for the views

logger = logging.getLogger("sendstack")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def config():
    """The connector's configuration, or None when it cannot even be loaded."""
    from workeasy360_sso.conf import ConfigurationError, get_config

    try:
        return get_config()
    except ConfigurationError:
        return None


def enabled() -> bool:
    """WorkEasy360 sign-in is configured (an issuer, a client and a credential)."""
    cfg = config()
    return bool(cfg is not None and cfg.is_configured)


def sso_only() -> bool:
    """WORKEASY360_SSO_ONLY: WorkEasy360 is the only way in.

    Read from the connector when it loads, and from the environment when it
    does not: a broken WorkEasy360 configuration must never quietly reopen
    password sign-in on a deployment that switched it off.
    """
    cfg = config()
    if cfg is not None:
        return bool(cfg.sso_only)
    return _env_bool("WORKEASY360_SSO_ONLY")


def provision_tenants() -> bool:
    """WORKEASY360_PROVISION_TENANTS (default true): a first sign-in creates the workspace."""
    return _env_bool("WORKEASY360_PROVISION_TENANTS", True)


def link_for_tenant(tenant_id):
    """The OrganizationLink of a workspace, or None."""
    if not tenant_id:
        return None
    from .models import OrganizationLink

    return OrganizationLink.objects.select_related("tenant").filter(tenant_id=tenant_id).first()


def linked_organization_id(tenant_id):
    link = link_for_tenant(tenant_id)
    return link.organization_id if link else None


def is_linked_tenant(tenant_id) -> bool:
    if not tenant_id:
        return False
    from .models import OrganizationLink

    return OrganizationLink.objects.filter(tenant_id=tenant_id).exists()


def has_workeasy360_identity(user) -> bool:
    """The account is a WorkEasy360 person's: it is used through WorkEasy360 only."""
    if user is None or not getattr(user, "pk", None):
        return False
    from workeasy360_sso.models import FederatedIdentity

    return FederatedIdentity.objects.filter(user_id=user.pk).exists()


def managed_by_workeasy360(user) -> bool:
    """A WorkEasy360 person, or anybody in a workspace linked to a WorkEasy360 organization.

    Nobody outside the organization -- a reseller above it, an operator
    impersonating -- may act as such an account.
    """
    if user is None:
        return False
    return has_workeasy360_identity(user) or is_linked_tenant(getattr(user, "tenant_id", None))


def revoke_password_sessions(user) -> int:
    """End every SimpleJWT refresh token of an account that now signs in through WorkEasy360."""
    from apps.accounts.sessions import revoke_all_refresh_tokens

    return revoke_all_refresh_tokens(user)


def parse_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


@transaction.atomic
def link_organization(*, tenant, organization_id, actor: str, detach_from_reseller: bool = False):
    """Point a WorkEasy360 organization's sign-ins at an existing workspace.

    Refuses, and changes nothing, when:

    * the workspace is not an active customer workspace (never the platform's
      own tenant, never a reseller's: their users manage other workspaces,
      and a WorkEasy360 session is one organization's and nobody else's);
    * the workspace hangs under a reseller, unless ``detach_from_reseller``
      moves it to the platform tenant first -- a reseller must not stand
      above a WorkEasy360 organization's workspace;
    * the organization or the workspace is already linked to something else.

    Returns the OrganizationLink.
    """
    from apps.audit.models import AuditEvent
    from apps.tenants.models import Tenant

    from .models import OrganizationLink

    organization_id = parse_uuid(organization_id)
    if organization_id is None:
        raise ValueError("The WorkEasy360 organization id must be a UUID.")
    tenant = Tenant.objects.select_for_update().get(pk=tenant.pk)
    if tenant.kind != Tenant.Kind.CUSTOMER:
        raise ValueError("Only a customer workspace can be linked (not the platform's or a reseller's).")
    if not tenant.is_active:
        raise ValueError("The workspace is suspended; reactivate it first.")

    existing = OrganizationLink.objects.filter(organization_id=organization_id).first()
    if existing is not None:
        if existing.tenant_id == tenant.pk:
            return existing
        raise ValueError(
            f"Organization {organization_id} already signs in to workspace {existing.tenant_id}. "
            "A linked organization is never re-pointed."
        )
    if OrganizationLink.objects.filter(tenant=tenant).exists():
        raise ValueError(f"Workspace {tenant.pk} is already linked to another WorkEasy360 organization.")

    from apps.accounts.models import User

    reseller_users = User.objects.filter(tenant=tenant).exclude(role=User.Role.USER)
    if reseller_users.exists():
        raise ValueError("The workspace has reseller or platform accounts in it; it cannot be linked.")

    parent = tenant.parent
    if parent is not None and parent.kind != Tenant.Kind.PLATFORM:
        if not detach_from_reseller:
            raise ValueError(
                f"Workspace {tenant.pk} is a customer of reseller tenant {parent.pk}. Run again with "
                "--detach-from-reseller to move it under the platform tenant first."
            )
        platform = Tenant.objects.filter(kind=Tenant.Kind.PLATFORM).order_by("pk").first()
        if platform is None:
            raise ValueError("There is no platform tenant to move the workspace under.")
        previous_parent = parent.pk
        tenant.parent = platform
        tenant.save(update_fields=["parent", "updated_at"])
        AuditEvent.objects.create(
            tenant=tenant, action="workeasy360.detached_from_reseller",
            object_type="tenants.Tenant", object_id=str(tenant.pk),
            metadata={"previous_parent": previous_parent, "actor": actor[:150]},
        )

    link = OrganizationLink.objects.create(
        organization_id=organization_id, tenant=tenant,
        origin=OrganizationLink.Origin.OPERATOR, linked_by=actor[:150],
    )
    AuditEvent.objects.create(
        tenant=tenant, action="workeasy360.organization_linked",
        object_type="workeasy360.OrganizationLink", object_id=str(link.pk),
        metadata={"organization_id": str(organization_id), "actor": actor[:150], "origin": link.origin},
    )
    logger.info("WorkEasy360 organization %s linked to workspace %s by %s.", organization_id, tenant.pk, actor)
    return link
