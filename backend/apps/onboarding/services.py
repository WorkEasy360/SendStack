"""
Deriving onboarding progress from what the workspace actually contains.

The design decision worth explaining is that **milestones are observed, not
reported**. Nothing calls `mark_whatsapp_connected()`. Instead `refresh()`
asks the underlying tables the same questions a person would -- is there a
connected number, is there a contact we are allowed to message, did a message
actually reach the provider -- and stamps whatever has newly become true.

Three things fall out of that, and each of them is why it is worth the extra
queries:

* **A client cannot forge progress.** There is no endpoint that marks a step
  done, so there is nothing to call with curl. The activation metric measures
  the product being used rather than a checkbox being ticked.
* **It is self-healing.** A workspace that connected WhatsApp during a deploy
  that dropped a signal is still correct the next time anybody looks. A
  signal-based design would have lost that milestone for ever.
* **It works for the customers who came before it.** An existing workspace
  with a year of campaigns gets a fully ticked checklist the first time it is
  read, rather than being told to go and connect the WhatsApp number it has
  been using since March.

The cost is a handful of `EXISTS` queries on the dashboard. `refresh` returns
immediately once every milestone is stamped, so a settled workspace pays one
indexed lookup and no more.
"""

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import FunnelEvent, OnboardingState

logger = logging.getLogger("sendstack")

#: Milestone field -> the funnel event recorded the moment it first becomes
#: true. One table drives both, so the checklist and the funnel can never
#: disagree about what happened.
_MILESTONE_EVENTS = {
    "started_at": FunnelEvent.Name.ONBOARDING_STARTED,
    "legal_accepted_at": FunnelEvent.Name.LEGAL_ACCEPTED,
    "whatsapp_connected_at": FunnelEvent.Name.WHATSAPP_CONNECTED,
    "contacts_added_at": FunnelEvent.Name.CONTACT_ADDED,
    "consent_confirmed_at": FunnelEvent.Name.CONSENT_CONFIRMED,
    "test_message_sent_at": FunnelEvent.Name.TEST_MESSAGE_SENT,
    "first_campaign_created_at": FunnelEvent.Name.FIRST_CAMPAIGN_CREATED,
    "first_campaign_sent_at": FunnelEvent.Name.FIRST_CAMPAIGN_SENT,
}

#: What a workspace must have done before it counts as activated. Named here
#: rather than buried in an `if`, because this is a product definition and
#: somebody will want to read it without reading the code around it.
ACTIVATION_REQUIREMENTS = ("whatsapp_connected_at", "consent_confirmed_at", "test_message_sent_at")


# ---------------------------------------------------------------------------
# The state row
# ---------------------------------------------------------------------------

def state_for(tenant, *, guided: bool = False) -> OnboardingState:
    """
    This workspace's onboarding row, created if it has none.

    `guided` is only ever applied when the row is *created*. A workspace that
    predates onboarding gets its row the first time somebody loads a
    dashboard, and it must be created un-guided -- see the field comment on
    `OnboardingState.is_guided`. Signup passes ``guided=True`` because a
    workspace created by signup genuinely is new.
    """
    tenant_id = getattr(tenant, "pk", tenant)
    state, _ = OnboardingState.objects.get_or_create(
        tenant_id=tenant_id, defaults={"is_guided": guided, "started_at": timezone.now()},
    )
    return state


# ---------------------------------------------------------------------------
# Observing the world
# ---------------------------------------------------------------------------

def _observe(tenant_id) -> dict:
    """
    Which milestones are true right now, read from the tables that own them.

    Every one is an `EXISTS`, not a `COUNT`: the question is "is there at
    least one", and counting a million contacts to answer it would be an
    expensive way to get the same boolean.
    """
    from apps.campaigns.models import Campaign
    from apps.contacts.models import Contact
    from apps.messaging.models import Message
    from apps.whatsapp.models import WhatsAppAccount

    observed = {"started_at": True}

    observed["legal_accepted_at"] = _legal_accepted(tenant_id)

    observed["whatsapp_connected_at"] = WhatsAppAccount.objects.filter(
        tenant_id=tenant_id, status=WhatsAppAccount.Status.CONNECTED,
    ).exists()

    observed["contacts_added_at"] = Contact.objects.filter(tenant_id=tenant_id).exists()

    observed["consent_confirmed_at"] = _has_eligible_contact(tenant_id)

    # A message that *actually went*. QUEUED is an intention; SENT is the
    # provider having accepted it. The distinction is the whole value of this
    # milestone -- a workspace whose every send fails has not activated, and a
    # checklist that ticked on "queued" would say it had.
    observed["test_message_sent_at"] = Message.objects.filter(
        tenant_id=tenant_id,
        status__in=(Message.Status.SENT, Message.Status.DELIVERED, Message.Status.READ),
    ).exists()

    observed["first_campaign_created_at"] = Campaign.objects.filter(tenant_id=tenant_id).exists()

    observed["first_campaign_sent_at"] = Campaign.objects.filter(
        tenant_id=tenant_id,
        status__in=(Campaign.Status.RUNNING, Campaign.Status.COMPLETED),
    ).exists()

    return observed


def _legal_accepted(tenant_id) -> bool:
    """Whether somebody in this workspace has accepted every required document, at its current version."""
    from apps.legal.documents_registry import ACCEPTANCE_REQUIRED, current_version
    from apps.legal.models import LegalAcceptance

    wanted = {(slug, current_version(slug)) for slug in ACCEPTANCE_REQUIRED}
    if not wanted:
        return True
    accepted = set(
        LegalAcceptance.objects.filter(
            tenant_id=tenant_id, document__in=[slug for slug, _ in wanted],
        ).values_list("document", "version")
    )
    return wanted.issubset(accepted)


def _has_eligible_contact(tenant_id) -> bool:
    """
    Whether this workspace has at least one contact it may actually market to.

    Asks the compliance rules rather than reading `subscribed` directly, so a
    workspace that requires opt-in consent is not told it has confirmed
    consent because it holds a thousand contacts whose consent is UNKNOWN.
    That is the difference between a checklist that is useful and one that
    walks a customer into a wall of blocked sends.
    """
    from apps.compliance.models import CompliancePolicy
    from apps.contacts.models import Contact

    policy = CompliancePolicy.for_tenant(tenant_id)
    eligible = Contact.objects.filter(tenant_id=tenant_id, subscribed=True)
    if policy.require_marketing_consent:
        eligible = eligible.filter(marketing_consent=Contact.MarketingConsent.GRANTED)
    return eligible.exists()


# ---------------------------------------------------------------------------
# Recording what was observed
# ---------------------------------------------------------------------------

def refresh(tenant, *, guided: bool = False, now=None) -> OnboardingState:
    """
    Bring a workspace's onboarding row up to date with reality, and return it.

    Stamps only fields that are NULL and have become true. A milestone is
    never un-stamped: a customer who disconnects their WhatsApp number in
    March has still connected one, and rewriting history so the checklist
    un-ticks would be both wrong and demoralising.
    """
    now = now or timezone.now()
    state = state_for(tenant, guided=guided)

    # The common case, and the reason this is cheap enough to call on every
    # dashboard load: a settled workspace has everything stamped and there is
    # nothing to look up.
    outstanding = [field for field in _MILESTONE_EVENTS if getattr(state, field) is None]
    if not outstanding and state.activated_at is not None:
        return state

    observed = _observe(state.tenant_id)

    changed = []
    for field in outstanding:
        if observed.get(field):
            setattr(state, field, now)
            changed.append(field)

    activated = state.activated_at is None and all(
        getattr(state, field) is not None for field in ACTIVATION_REQUIREMENTS
    )
    if activated:
        state.activated_at = now
        changed.append("activated_at")

    if changed:
        state.save(update_fields=changed + ["updated_at"])
        for field in changed:
            event = _MILESTONE_EVENTS.get(field)
            if event is not None:
                record(event, tenant=state.tenant_id, occurred_at=now)
        if activated:
            record(FunnelEvent.Name.ACTIVATED, tenant=state.tenant_id, occurred_at=now)
            logger.info("Workspace %s activated.", state.tenant_id)

    return state


def dismiss(tenant) -> OnboardingState:
    """The customer has asked to stop being shown the checklist. Records nothing else."""
    state = state_for(tenant)
    if state.dismissed_at is None:
        state.dismissed_at = timezone.now()
        state.save(update_fields=["dismissed_at", "updated_at"])
    return state


# ---------------------------------------------------------------------------
# The funnel
# ---------------------------------------------------------------------------

def record(name, *, tenant=None, occurred_at=None, **metadata) -> FunnelEvent | None:
    """
    Note that something happened on the way to becoming a customer.

    Never raises and never blocks the thing it is describing. A funnel event
    that cannot be written is a gap in a chart; a funnel event that throws
    would be a failed signup.

    Once-per-workspace events are enforced by the database, so the second
    attempt returns None rather than a second row -- which is exactly what
    "record the first campaign" should do when called for the second campaign.

    **Metadata is counts and names only.** No phone numbers, no message text,
    no addresses, no tokens. See the model docstring.
    """
    tenant_id = getattr(tenant, "pk", tenant)
    try:
        with transaction.atomic():
            return FunnelEvent.objects.create(
                tenant_id=tenant_id,
                name=name,
                occurred_at=occurred_at or timezone.now(),
                metadata=metadata or {},
            )
    except IntegrityError:
        # Already recorded. The expected outcome for a milestone, not an error.
        return None
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("Could not record the funnel event %s.", name)
        return None


def funnel_counts(*, since=None, until=None) -> dict:
    """
    How many workspaces reached each stage, for the operator's funnel view.

    Counts **distinct workspaces**, not events: the question is how many
    customers got this far, and an event count would let one workspace's three
    upgrade attempts look like three customers.
    """
    from django.db.models import Count

    events = FunnelEvent.objects.all()
    if since is not None:
        events = events.filter(occurred_at__gte=since)
    if until is not None:
        events = events.filter(occurred_at__lt=until)

    rows = events.values("name").annotate(
        workspaces=Count("tenant", distinct=True), events=Count("id"),
    )
    return {row["name"]: {"workspaces": row["workspaces"], "events": row["events"]} for row in rows}
