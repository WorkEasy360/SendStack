"""
Self-service registration: one form, one workspace, one owner.

Everything a new account is made of is decided **here, on the server**. The
request supplies a name, an email, a password, a company name and at most the
*name* of a public plan. It does not supply, and cannot influence:

* which tenant the workspace hangs off,
* what kind of tenant it is,
* who owns it,
* what role the new user gets,
* which plan the trial actually runs on,
* what that plan costs or permits.

That list is the security model of this module. A registration endpoint that
accepted ``tenant_id`` or ``role`` from the body would be a self-service
privilege escalation, and it is the single most likely way for a
commercialization phase to undo three phases of tenant isolation.

The other thing worth stating plainly is what registration is **not**: it is
not a payment. A new customer lands on a trial (or, if trials are switched
off, on nothing at all and is told so). Nobody's plan is activated by signing
a form.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from apps.legal import services as legal
from apps.legal.documents_registry import ACCEPTANCE_REQUIRED
from apps.onboarding import services as onboarding
from apps.onboarding.models import FunnelEvent
from apps.subscriptions import trials
from apps.subscriptions.models import Plan
from apps.tenants.models import Tenant

from .models import PLATFORM_TENANT_SLUG

logger = logging.getLogger("sendstack")

#: The one message a failed registration ever gives about an address.
#:
#: Deliberately does not say whether the address is already registered. Signup
#: is unauthenticated and unlimited-audience, so a response that distinguished
#: "taken" from "invalid" would be a free membership oracle for any list of
#: addresses -- and this deployment already went to the trouble of making
#: password reset generic (Phase 3), which the same attacker would otherwise
#: simply walk around through this endpoint instead.
GENERIC_EMAIL_REFUSAL = (
    "We could not create an account with those details. If you already have an account, "
    "sign in or reset your password."
)


class RegistrationDisabled(Exception):
    """Self-service signup is switched off in this deployment."""


class RegistrationRefused(Exception):
    """The details were not acceptable. Carries the message the customer should see."""


def signup_enabled() -> bool:
    return bool(getattr(settings, "SELF_SERVICE_SIGNUP_ENABLED", False))


def normalize_email(raw: str) -> str:
    """
    Lowercased and stripped, whole address.

    Django's own `normalize_email` lowercases only the domain, on the correct
    reading that the local part is case-sensitive per RFC 5321. That reading
    is right about mail and wrong about accounts: no real provider treats
    ``Alice@`` and ``alice@`` as two people, and honouring the RFC here would
    let one person hold two accounts on one mailbox and let a duplicate check
    be defeated by pressing shift.
    """
    return (raw or "").strip().lower()


# ---------------------------------------------------------------------------
# Where a self-service workspace hangs
# ---------------------------------------------------------------------------

def signup_parent_tenant() -> Tenant:
    """
    The tenant a self-service customer is created under.

    The platform tenant by default -- a direct customer of whoever runs the
    deployment. ``SELF_SERVICE_SIGNUP_PARENT_SLUG`` can point signups at a
    reseller instead, for a deployment where the public website belongs to
    one. Never a customer tenant: a customer under a customer is not a shape
    this hierarchy has, and it would put one customer's data inside another's
    subtree.
    """
    slug = (getattr(settings, "SELF_SERVICE_SIGNUP_PARENT_SLUG", "") or "").strip()
    if slug:
        parent = Tenant.objects.filter(
            slug=slug, kind__in=(Tenant.Kind.PLATFORM, Tenant.Kind.RESELLER), is_active=True,
        ).first()
        if parent is not None:
            return parent
        # Configured and wrong is a deployment error, not a reason to silently
        # attach a paying customer to the wrong parent. Fall through to the
        # platform tenant and say so loudly.
        logger.error(
            "SELF_SERVICE_SIGNUP_PARENT_SLUG names no active platform or reseller tenant; "
            "using the platform tenant instead.",
        )

    platform = Tenant.objects.filter(kind=Tenant.Kind.PLATFORM).order_by("pk").first()
    if platform is None:
        platform = Tenant.objects.create(
            name="SendStack Platform", slug=PLATFORM_TENANT_SLUG, kind=Tenant.Kind.PLATFORM,
        )
    return platform


# ---------------------------------------------------------------------------
# Naming a new workspace
# ---------------------------------------------------------------------------

def unique_workspace_identity(name: str, parent) -> tuple:
    """
    A (name, slug) pair that will not collide, given what already exists.

    Two constraints have to be satisfied at once: `Tenant.slug` is unique
    across the whole table, and `(parent, name)` is unique among siblings.
    Two businesses genuinely called "Sharma Traders" both signing up is not an
    error and must not be one, so the second gets "Sharma Traders 2".

    Bounded rather than a `while True`: a hostile signup loop must not be able
    to make this spin. Past the ceiling it falls back to a random suffix,
    which collides with probability nobody needs to think about.
    """
    import secrets

    base_name = (name or "").strip()[:140] or "Workspace"
    base_slug = slugify(base_name)[:140] or "workspace"

    for attempt in range(1, 25):
        candidate_name = base_name if attempt == 1 else f"{base_name} {attempt}"
        candidate_slug = base_slug if attempt == 1 else f"{base_slug}-{attempt}"
        name_taken = Tenant.objects.filter(parent=parent, name=candidate_name).exists()
        slug_taken = Tenant.objects.filter(slug=candidate_slug).exists()
        if not name_taken and not slug_taken:
            return candidate_name, candidate_slug

    token = secrets.token_hex(3)
    return f"{base_name} {token}", f"{base_slug}-{token}"


def unique_username(email: str) -> str:
    """
    A login name for a new account, derived from their address.

    The email itself where it is free, because that is what somebody types
    into a login form without being told. `AbstractUser.username` allows the
    characters an address uses and is 150 wide, so this is a straight copy in
    the ordinary case.
    """
    import secrets

    candidate = email[:150]
    user_model = get_user_model()
    if not user_model.objects.filter(username__iexact=candidate).exists():
        return candidate
    for _ in range(10):
        suffixed = f"{email[:140]}.{secrets.token_hex(3)}"[:150]
        if not user_model.objects.filter(username__iexact=suffixed).exists():
            return suffixed
    raise RegistrationRefused(GENERIC_EMAIL_REFUSAL)


# ---------------------------------------------------------------------------
# Plan selection
# ---------------------------------------------------------------------------

def resolve_requested_plan(plan_name):
    """
    Turn what the browser asked for into a plan this deployment is willing to sell.

    **The name is a preference, not an instruction.** It is matched against
    platform-owned, active, *public* plans and nothing else, so a caller
    cannot name a reseller's negotiated tier, a disabled plan, or an internal
    one. Price, limits and features all come from the row that comes back --
    never from the request, which is why the request has no fields for them.

    An unknown name resolves to None and the customer gets the ordinary trial
    plan, because a signup is not worth failing over a stale link.
    """
    name = (plan_name or "").strip()
    if not name:
        return None
    return Plan.objects.filter(
        owner__isnull=True, name__iexact=name, is_active=True, is_public=True,
    ).first()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@transaction.atomic
def register(
    *,
    email: str,
    password: str,
    full_name: str,
    company: str,
    plan_name: str = "",
    accepted_terms: bool = False,
    marketing_opt_in: bool = False,
    request=None,
):
    """
    Create a workspace, its owner and its trial, atomically.

    One transaction, so a failure anywhere leaves nothing behind: half a
    signup -- a user with no tenant, a tenant with no subscription -- is worse
    than no signup, because the customer cannot retry past it and no screen in
    the product knows what to do with it.

    Returns ``(user, tenant, subscription)``. `subscription` is None when the
    deployment has trials switched off, which is a configuration state the
    caller must render rather than an error.
    """
    if not signup_enabled():
        raise RegistrationDisabled(
            "Self-service signup is not available on this deployment."
        )

    if not accepted_terms:
        # Checked here as well as in the serializer, because this is the layer
        # that writes the acceptance record and the record must never be
        # written for somebody who did not tick the box.
        raise RegistrationRefused("You must accept the Terms of Service and Privacy Policy to create an account.")

    email = normalize_email(email)
    user_model = get_user_model()

    # Case-insensitive on both columns. `email` is not unique at the database
    # level on AbstractUser, so this check is the constraint -- and it runs
    # inside the transaction, which is what stops two simultaneous signups on
    # one address from both succeeding: the second blocks on the first's
    # row locks in `create_user` and then fails its own IntegrityError on
    # username, which is caught below.
    if user_model.objects.filter(email__iexact=email).exists():
        raise RegistrationRefused(GENERIC_EMAIL_REFUSAL)

    try:
        validate_password(password)
    except DjangoValidationError as exc:
        raise RegistrationRefused(" ".join(exc.messages)) from exc

    parent = signup_parent_tenant()
    workspace_name, workspace_slug = unique_workspace_identity(company, parent)

    tenant = Tenant.objects.create(
        name=workspace_name,
        slug=workspace_slug,
        # Hard-coded, and it is the point of the line. A self-service signup
        # produces a customer. Never a reseller, whatever the body said --
        # there is no field for it and there must never be one.
        kind=Tenant.Kind.CUSTOMER,
        parent=parent,
    )

    first_name, _, last_name = (full_name or "").strip().partition(" ")
    try:
        user = user_model.objects.create_user(
            username=unique_username(email),
            email=email,
            password=password,
            first_name=first_name[:150],
            last_name=last_name.strip()[:150],
            # The ordinary customer role. Not SUPERADMIN, not RESELLER -- see
            # the module docstring. This user administers their own workspace
            # and nothing above or beside it.
            role=user_model.Role.USER,
            tenant=tenant,
            is_staff=False,
            is_superuser=False,
        )
    except IntegrityError as exc:
        # A concurrent signup won the race for this username. Same neutral
        # message as any other duplicate, and the transaction rolls the
        # half-made workspace back with it.
        raise RegistrationRefused(GENERIC_EMAIL_REFUSAL) from exc

    # The workspace's own record of who agreed to what, at which version. The
    # version comes from the server (see apps.legal.services), so a client
    # cannot claim to have accepted wording it was never shown.
    for slug in ACCEPTANCE_REQUIRED:
        legal.record_acceptance(user, slug, request=request)

    # Marketing consent is a **separate** decision from accepting the terms,
    # stored in its own column, and defaulted off. Bundling the two would make
    # the legal acceptance record double as evidence of something it is not,
    # and pre-ticking the marketing box is on the short list of things this
    # phase must not do.
    if marketing_opt_in:
        user.marketing_opt_in = True
        user.save(update_fields=["marketing_opt_in", "updated_at"])

    subscription = None
    if trials.trials_enabled():
        plan = resolve_requested_plan(plan_name)
        try:
            subscription = trials.start_trial(tenant, plan=plan)
        except trials.TrialUnavailable:
            logger.exception("Trials are enabled but no trial plan resolved for tenant %s.", tenant.pk)

    state = onboarding.state_for(tenant, guided=True)
    onboarding.record(FunnelEvent.Name.SIGNUP_COMPLETED, tenant=tenant)
    if subscription is not None:
        onboarding.record(
            FunnelEvent.Name.TRIAL_STARTED, tenant=tenant, plan=subscription.plan.name,
        )
    onboarding.record(FunnelEvent.Name.ONBOARDING_STARTED, tenant=tenant)

    logger.info(
        "Registered workspace %s (tenant id=%s) with %s.",
        tenant.slug, tenant.pk, "a trial" if subscription else "no subscription",
    )
    return user, tenant, subscription, state
