"""
Per-recipient WhatsApp account selection and per-message send delays.

Two independent knobs live here, both resolved per campaign with a
deployment-wide fallback (see settings.CAMPAIGN_*):

  * **Which account sends a given recipient's message.** MANUAL campaigns
    always use ``campaign.whatsapp_account``. AUTO campaigns deal recipients
    across every *connected* account in the tenant.

  * **How long to wait before each individual message.** Every message gets
    its own freshly generated delay -- never one delay computed once and
    reused for the whole campaign.

Thread/process safety: nothing here mutates shared state. Assignment for a
campaign's initial audience happens once, inside the ``launch()``
transaction; the send-time re-selection below is a pure function of the
account statuses visible at that instant, so two workers running it
concurrently simply make two independently valid choices. The write that
acts on a re-selection is a conditional UPDATE guarded on the message still
being QUEUED (see apps/messaging/services.py), which is what actually
prevents two workers from both acting on the same recipient.
"""

import logging
import random

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.whatsapp.models import WhatsAppAccount

logger = logging.getLogger("sendstack")

#: A generated delay must be strictly greater than zero, so that no two
#: messages are ever scheduled for the same instant. A campaign configured
#: with a minimum of 0 still waits at least this long.
MIN_DELAY_FLOOR_SECONDS = 0.01

#: Delays are rounded to this many decimal places before being persisted and
#: logged -- sub-millisecond precision is noise nobody can act on, and round
#: numbers make the campaign report readable.
_DELAY_PRECISION = 3


def resolve_tenant_delay_bounds(tenant_settings) -> tuple[float, float]:
    """
    A workspace's own delay window, resolved against the deployment default.

    Split out from `resolve_delay_bounds` so the settings screen can show what
    a workspace's preference actually amounts to without inventing a campaign
    to ask about.
    """
    minimum, maximum = tenant_settings.resolved_delay_bounds()
    if minimum is None:
        minimum = settings.CAMPAIGN_MIN_DELAY_SECONDS
    if maximum is None:
        maximum = settings.CAMPAIGN_MAX_DELAY_SECONDS
    return max(float(minimum), 0.0), max(float(maximum), 0.0)


def resolve_delay_bounds(campaign) -> tuple[float, float]:
    """
    The (min, max) delay window for one campaign, in seconds.

    Three levels, most specific first:

        this campaign  >  this workspace's settings  >  the deployment default

    Each is consulted only where the one before it said nothing, which is what
    makes "leave it blank to use the account default" mean what it says. A
    campaign created before either of the first two existed has both blank and
    resolves to the deployment default, exactly as it always did.
    """
    minimum = campaign.min_delay_seconds
    maximum = campaign.max_delay_seconds

    if minimum is None or maximum is None:
        from apps.tenants.models import TenantSettings

        workspace_min, workspace_max = resolve_tenant_delay_bounds(
            TenantSettings.for_tenant(campaign.tenant_id)
        )
        if minimum is None:
            minimum = workspace_min
        if maximum is None:
            maximum = workspace_max

    minimum = max(float(minimum), 0.0)
    maximum = max(float(maximum), 0.0)
    # A max below the min is meaningless; the serializer rejects it on input,
    # but a config change can still leave a stored campaign inconsistent, and
    # random.uniform() would silently draw from the reversed range.
    if maximum < minimum:
        maximum = minimum
    return minimum, maximum


def next_delay(campaign) -> float:
    """
    Generate the delay for exactly **one** message.

    Call this once per message, immediately before that message is
    scheduled -- never once per campaign. The result always satisfies
    ``0 < delay <= max``.
    """
    minimum, maximum = resolve_delay_bounds(campaign)
    delay = random.uniform(minimum, maximum)
    # Clamp after rounding, not before: rounding 0.0004 down to 0.0 would
    # otherwise break the "strictly greater than zero" guarantee.
    return max(round(delay, _DELAY_PRECISION), MIN_DELAY_FLOOR_SECONDS)


def _not_paused() -> Q:
    """
    Excludes numbers whose sending is currently paused.

    A paused number is unavailable for the same practical reason a
    disconnected one is -- nothing will go out of it -- so an automatic
    campaign should deal its recipients to the others rather than pile
    messages up behind it. An expired pause is not a pause: the `paused_until`
    clause is what lets a number that paused itself an hour ago come back into
    the pool without anyone having to clear it.
    """
    now = timezone.now()
    return ~(
        Q(sending_policy__is_paused=True)
        & (Q(sending_policy__paused_until__isnull=True) | Q(sending_policy__paused_until__gt=now))
    )


def eligible_accounts(campaign) -> list[WhatsAppAccount]:
    """
    Accounts this campaign may send from right now, newest state from the DB.

    Never returns a disconnected, errored, expired or still-connecting
    account -- only ``CONNECTED`` ones, which is the single condition
    ConnectionManager also enforces before it will dispatch a send -- and
    never one whose sending is paused (see apps.whatsapp.health).
    """
    if campaign.account_assignment_mode != campaign.AccountAssignmentMode.AUTO:
        account = campaign.whatsapp_account
        if not account or account.status != WhatsAppAccount.Status.CONNECTED:
            return []
        return list(WhatsAppAccount.objects.filter(pk=account.pk).filter(_not_paused()))

    return list(
        WhatsAppAccount.objects.filter(
            tenant_id=campaign.tenant_id,
            status=WhatsAppAccount.Status.CONNECTED,
        ).filter(_not_paused()).order_by("id")
    )


def assign_accounts(campaign, count: int) -> list[WhatsAppAccount]:
    """
    Deal ``count`` recipients across this campaign's eligible accounts.

    AUTO mode uses a *shuffled* round robin: each pass over the account pool
    is reshuffled, so the order accounts appear in is randomised and not
    predictable from the recipient's position, while the totals per account
    still come out even to within one message. Both properties matter -- a
    plain ``random.choice`` per recipient is randomised but routinely leaves
    one account with twice another's share, and a plain round robin
    distributes evenly but always starts from the same account.

    Returns an empty list when nothing is eligible; callers decide whether
    that's a launch-time error or a per-message skip.
    """
    accounts = eligible_accounts(campaign)
    if not accounts or count <= 0:
        return []

    if len(accounts) == 1:
        return [accounts[0]] * count

    dealt: list[WhatsAppAccount] = []
    while len(dealt) < count:
        cycle = list(accounts)
        random.shuffle(cycle)
        dealt.extend(cycle)
    return dealt[:count]


def reselect_account(campaign, unavailable_account_id) -> WhatsAppAccount | None:
    """
    Pick a replacement account for a message whose assigned one went away.

    Only AUTO campaigns get a replacement: a MANUAL campaign was explicitly
    told which account to use, so silently sending from a different number
    would not be a recovery, it would be a surprise. Returns None when no
    other eligible account exists.
    """
    if campaign.account_assignment_mode != campaign.AccountAssignmentMode.AUTO:
        return None

    candidates = [account for account in eligible_accounts(campaign) if account.pk != unavailable_account_id]
    if not candidates:
        return None
    return random.choice(candidates)


def describe_distribution(assignments) -> str:
    """Compact "Name(id)=n, ..." summary of an assignment, for the launch log."""
    tally: dict[tuple, int] = {}
    for account in assignments:
        key = (account.pk, account.name)
        tally[key] = tally.get(key, 0) + 1
    return ", ".join(f"{name}({pk})={total}" for (pk, name), total in sorted(tally.items()))
