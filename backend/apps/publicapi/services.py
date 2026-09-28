"""
What the public endpoints actually do.

The whole design rule of this module is in what it does *not* contain. There is
no send loop, no provider call, no credit arithmetic, no consent check, no
template renderer and no campaign dispatcher. Every one of those already exists
and is already the thing the SPA uses, and a second copy of any of them would
be a second set of rules that drifts.

What is here is translation and resolution: turn a public request into the
arguments the existing services already take, resolve public ids to rows this
tenant is allowed to touch, and hand off. A public send ends up in exactly the
same `Message` row, the same `send_message_task`, the same
`apps.messaging.gating` evaluation and the same credit ledger as a reply typed
into the inbox.
"""

import logging

from django.db import transaction
from django.utils import timezone

from apps.campaigns.models import Campaign
from apps.contacts.models import Contact
from apps.contacts.upsert import ContactQuotaReached, upsert_contact
from apps.contacts.utils import normalize_phone
from apps.credits.services import InsufficientCreditsError, reserve_credits
from apps.messaging.models import Message
from apps.templates_app.models import MessageTemplate
from apps.whatsapp.models import WhatsAppAccount

from . import keys as key_utils
from .errors import (
    ACCOUNT_DISCONNECTED,
    ACCOUNT_NOT_FOUND,
    CAMPAIGN_STATE_INVALID,
    INSUFFICIENT_CREDITS,
    INVALID_RECIPIENT,
    INVALID_REQUEST,
    INVALID_TEMPLATE,
    MESSAGE_FAILED,
    NOT_FOUND,
    QUOTA_EXCEEDED,
    TEST_KEY_CANNOT_SEND,
    PublicAPIError,
)
from .models import ApiKey
from .scopes import validate as validate_scopes
from .serializers import (
    ACCOUNT_PREFIX,
    CAMPAIGN_PREFIX,
    PUBLIC_MESSAGE_TYPES,
    TEMPLATE_PREFIX,
    parse_public_id,
)

logger = logging.getLogger("sendstack")


# --- API key lifecycle ---------------------------------------------------


@transaction.atomic
def create_api_key(*, tenant, owner, created_by=None, name, scopes, environment="live",
                   expires_at=None, allowed_account_ids=(), requests_per_minute=0,
                   messages_per_minute=0, campaigns_per_day=0, rotated_from=None):
    """
    Mint a key and return ``(api_key, full_key)``.

    `full_key` is the **only** time the complete secret exists anywhere outside
    the caller's own memory. It is returned rather than stored, the caller shows
    it once, and there is deliberately no code path anywhere in this app that
    can produce it again.
    """
    full_key, prefix, hashed = key_utils.generate(environment)

    api_key = ApiKey.objects.create(
        tenant=tenant,
        owner=owner,
        created_by=created_by,
        name=name,
        environment=environment,
        key_prefix=prefix,
        hashed_secret=hashed,
        scopes=validate_scopes(list(scopes)),
        expires_at=expires_at,
        requests_per_minute=requests_per_minute or 0,
        messages_per_minute=messages_per_minute or 0,
        campaigns_per_day=campaigns_per_day or 0,
        rotated_from=rotated_from,
    )

    if allowed_account_ids:
        # Filtered by tenant, not merely by id. Without that filter this field
        # would be a way to name another workspace's device and have the send
        # path fetch it.
        accounts = WhatsAppAccount.objects.filter(tenant=tenant, pk__in=allowed_account_ids)
        found = {account.pk for account in accounts}
        missing = sorted(set(allowed_account_ids) - found)
        if missing:
            raise PublicAPIError(
                ACCOUNT_NOT_FOUND,
                f"No WhatsApp account in this workspace with id(s): {', '.join(str(m) for m in missing)}.",
            )
        api_key.allowed_accounts.set(accounts)

    return api_key, full_key


@transaction.atomic
def rotate_api_key(api_key, *, actor=None):
    """
    Replace a key's secret, keeping its identity.

    A **new row** rather than a new secret on the old one. The old key is
    revoked, and it has to survive: `ApiRequestLog.api_key` points at it, and
    rewriting the secret in place would leave months of request history
    attributed to a credential that no longer means what it did. The new row
    carries `rotated_from`, so the dashboard can show the lineage.

    The old key stops working *immediately*. A grace period would be friendlier
    and is the wrong default: rotation is what somebody does when they think a
    key leaked, and a leaked key that keeps working for an hour is an hour of
    access granted to whoever leaked it. A caller who wants overlap creates a
    second key first, migrates, then revokes the first.
    """
    now = timezone.now()

    replacement, full_key = create_api_key(
        tenant=api_key.tenant,
        owner=api_key.owner,
        created_by=actor,
        name=api_key.name,
        scopes=list(api_key.scopes or []),
        environment=api_key.environment,
        expires_at=api_key.expires_at,
        allowed_account_ids=[account.pk for account in api_key.allowed_accounts.all()],
        requests_per_minute=api_key.requests_per_minute,
        messages_per_minute=api_key.messages_per_minute,
        campaigns_per_day=api_key.campaigns_per_day,
        rotated_from=api_key,
    )

    ApiKey.objects.filter(pk=api_key.pk).update(
        revoked_at=now, is_active=False, rotated_at=now, updated_at=now,
    )
    return replacement, full_key


def revoke_api_key(api_key) -> None:
    """Permanent. See the ApiKey docstring for why this is not `is_active=False`."""
    if api_key.revoked_at is None:
        ApiKey.objects.filter(pk=api_key.pk).update(
            revoked_at=timezone.now(), is_active=False, updated_at=timezone.now(),
        )


def set_api_key_active(api_key, active: bool) -> None:
    """Reversible pause. A revoked key can never be re-enabled by this."""
    if api_key.revoked_at is not None and active:
        raise PublicAPIError(
            INVALID_REQUEST,
            "A revoked key cannot be re-enabled. Create a new key instead.",
        )
    ApiKey.objects.filter(pk=api_key.pk).update(is_active=active, updated_at=timezone.now())


# --- Resolution ----------------------------------------------------------
#
# Every one of these takes the api_key and filters by `api_key.tenant`. That is
# the isolation boundary, and it is applied in the query rather than checked
# after the fetch, so a miss is indistinguishable from "does not exist" -- a
# caller must not be able to probe for the existence of another tenant's ids.


def resolve_account(api_key, account_id=None, *, require_connected=True):
    """
    Which WhatsApp account this request sends from.

    With an `account_id`, that one -- if it is in this tenant and within the
    key's `allowed_accounts`. Without one, **the router picks**, which is the
    part that makes the public API provider-agnostic: an integration says
    "whatsapp" and never learns whether the message went out over the QR bridge
    or the Cloud API.
    """
    accounts = WhatsAppAccount.objects.filter(tenant=api_key.tenant)

    # `.exists()` rather than a truthiness test on the manager: an empty
    # allowed_accounts means "every account in this workspace", and the M2M
    # manager is always truthy.
    allowed_ids = list(api_key.allowed_accounts.values_list("pk", flat=True))
    if allowed_ids:
        accounts = accounts.filter(pk__in=allowed_ids)

    if account_id:
        pk = parse_public_id(account_id, ACCOUNT_PREFIX)
        if pk is None:
            raise PublicAPIError(ACCOUNT_NOT_FOUND, f"'{account_id}' is not a valid account id.", status_code=404)
        account = accounts.filter(pk=pk).first()
        if account is None:
            raise PublicAPIError(
                ACCOUNT_NOT_FOUND,
                f"No WhatsApp account '{account_id}' is available to this API key.",
                status_code=404,
            )
        if require_connected and not _sendable(account):
            raise PublicAPIError(
                ACCOUNT_DISCONNECTED,
                f"WhatsApp account '{account_id}' is not connected and cannot send.",
                status_code=409,
            )
        return account

    if not require_connected:
        account = accounts.first()
        if account is None:
            raise PublicAPIError(
                ACCOUNT_NOT_FOUND, "This workspace has no WhatsApp accounts.", status_code=404,
            )
        return account

    # No account named: choose one. Ordered by id so repeated sends from an
    # integration that never names an account land on the same number, which is
    # what a recipient expects to see a conversation continue from.
    for account in accounts.filter(status=WhatsAppAccount.Status.CONNECTED).order_by("pk"):
        if _sendable(account):
            return account

    raise PublicAPIError(
        ACCOUNT_DISCONNECTED,
        "No connected WhatsApp account is available to send from.",
        status_code=409,
    )


def _sendable(account) -> bool:
    """Connected and not paused -- the existing definition, not a second one."""
    from apps.messaging.services import account_is_sendable

    return account_is_sendable(account)


def resolve_template(api_key, template_id):
    pk = parse_public_id(template_id, TEMPLATE_PREFIX)
    template = None
    if pk is not None:
        template = MessageTemplate.objects.filter(tenant=api_key.tenant, pk=pk).first()
    if template is None:
        # Templates are also addressable by name, because that is what an
        # integration hard-codes: `"welcome_customer"` survives a database
        # restore and a numeric id does not.
        template = MessageTemplate.objects.filter(tenant=api_key.tenant, name=str(template_id)).first()
    if template is None:
        raise PublicAPIError(
            INVALID_TEMPLATE, f"No template '{template_id}' in this workspace.", status_code=404,
        )
    if not template.is_active:
        raise PublicAPIError(INVALID_TEMPLATE, f"Template '{template.name}' is inactive.")
    return template


def resolve_campaign(api_key, campaign_id):
    pk = parse_public_id(campaign_id, CAMPAIGN_PREFIX)
    campaign = None
    if pk is not None:
        campaign = Campaign.objects.filter(tenant=api_key.tenant, pk=pk).first()
    if campaign is None:
        raise PublicAPIError(
            NOT_FOUND, f"No campaign '{campaign_id}' in this workspace.", status_code=404,
        )
    return campaign


def resolve_recipient(api_key, raw_phone, *, name="", create=True):
    """
    Turn a phone number into a Contact row in this tenant.

    Creating the contact is not bookkeeping -- it is what makes the compliance
    gate work. Consent, opt-out and quiet hours are all evaluated against a
    Contact (see apps.messaging.gating), so a send with no contact attached is
    a send that silently skips the opt-out check. An integration must not be
    able to message somebody who unsubscribed simply by not knowing about them.
    """
    try:
        phone = normalize_phone(raw_phone)
    except ValueError as exc:
        raise PublicAPIError(INVALID_RECIPIENT, str(exc)) from exc

    contact = Contact.objects.filter(tenant=api_key.tenant, phone=phone).first()
    if contact is not None or not create:
        return contact, phone

    try:
        contact, _ = upsert_contact(
            tenant=api_key.tenant,
            phone=phone,
            defaults={"name": name or "", "created_by": api_key.owner},
            actor=api_key.owner,
        )
    except ContactQuotaReached as exc:
        raise PublicAPIError(QUOTA_EXCEEDED, str(exc.detail), status_code=402) from exc
    return contact, phone


# --- Sending -------------------------------------------------------------


def render_template(template, contact, variables: dict) -> dict:
    """
    Fill a template's variables for one recipient.

    Delegates to `apps.templates_app.services.render_template`, which is the
    renderer campaigns and automation already use. That matters more than it
    looks: it knows about both placeholder syntaxes, the computed variables
    (`first_name`, `date`, `time`), the precedence between a custom field and a
    computed one, and the tenant-local clock. A second renderer here would
    render the same template differently through the API than through the
    dashboard, which is exactly the class of bug an API façade is supposed to
    make impossible.

    The API's per-request `variables` are layered on top by writing them into
    the render context, so a caller can personalise a send without first
    updating the contact -- an order id belongs to the message, not the person.
    """
    from apps.templates_app.services import (
        build_render_context,
        render_buttons,
        render_interactive,
        render_text,
    )
    from core.media import resolve_media_items, spec_from_object

    context = build_render_context(contact) if contact is not None else {}
    # Lower-cased to match the context's own convention -- `build_render_context`
    # keys everything case-insensitively, and a caller sending "Name" must hit
    # the same slot the template's `[Name]` reads.
    for key, value in (variables or {}).items():
        context[str(key).lower()] = "" if value is None else value

    fallbacks = template.variables or {}
    return {
        "message_type": template.message_type,
        "body": render_text(template.body, context, fallbacks),
        "caption": render_text(template.caption, context, fallbacks),
        "media_url": template.media_url or "",
        "media_items": resolve_media_items(spec_from_object(template)),
        "filename": template.filename or "",
        "buttons": render_buttons(template.buttons, context, fallbacks),
        "list_content": render_interactive(template.list_content, context, fallbacks, kind="list"),
        "poll_content": render_interactive(template.poll_content, context, fallbacks, kind="poll"),
    }


def send_message(api_key, data: dict):
    """
    Queue one outbound message and return the `Message` row.

    Returns as soon as the row exists and the Celery task is published -- the
    provider is never called inside the request, which is what keeps this
    endpoint fast and what keeps a slow WhatsApp connection from turning into a
    slow HTTP API. Everything after this point is the existing send path:
    `send_message_task` -> `gating.evaluate` -> `attempt_send` -> the provider.
    """
    if api_key.environment == ApiKey.Environment.TEST:
        raise PublicAPIError(
            TEST_KEY_CANNOT_SEND,
            "This is a test key. Create a live key (ss_live_...) to send real messages.",
            status_code=403,
        )

    account = resolve_account(api_key, data.get("account_id"))
    contact, phone = resolve_recipient(api_key, data["to"], name=data.get("name", ""))

    template = None
    if data.get("template_id"):
        template = resolve_template(api_key, data["template_id"])
        content = render_template(template, contact, data.get("variables") or {})
    else:
        message_type = PUBLIC_MESSAGE_TYPES[(data.get("type") or "text").lower()]
        content = {
            "message_type": message_type,
            "body": data.get("text") or "",
            "media_url": data.get("media_url") or "",
            "media_items": [],
            "caption": data.get("caption") or "",
            "filename": data.get("filename") or "",
            "buttons": [],
            "list_content": {},
            "poll_content": {},
        }

    # **Every refusal happens before the reservation.** `_message_kind` raises on
    # a bad value, and reserving first would mean a request rejected for a typo
    # had already taken a credit off the balance -- released nowhere, because
    # no Message row exists for the send task to release it against.
    #
    # It is also resolved outside the try block below, which turns *any*
    # exception into a 500 MESSAGE_FAILED: a bad `kind` is a 400 the caller can
    # act on, and computing it in there would bury it as a server error.
    kind = _message_kind(data.get("kind"))

    # Reserved once nothing else can refuse, exactly as every other send path
    # does it, so a workspace with no credits is told here rather than
    # discovering it one worker-minute later. The reservation is released or
    # consumed by `send_message_task` -- this app adds no new credit rules.
    try:
        reserve_credits(user_id=api_key.owner_id, amount=1, reference=f"apikey:{api_key.pk}")
    except InsufficientCreditsError as exc:
        raise PublicAPIError(INSUFFICIENT_CREDITS, str(exc), status_code=402) from exc

    try:
        with transaction.atomic():
            message = Message.objects.create(
                tenant=api_key.tenant,
                whatsapp_account=account,
                contact=contact,
                template=template,
                to_phone=phone,
                direction=Message.Direction.OUTBOUND,
                message_type=content["message_type"],
                body=content["body"],
                media_url=content["media_url"],
                media_items=content["media_items"],
                caption=content["caption"],
                filename=content["filename"],
                buttons=content["buttons"],
                list_content=content["list_content"],
                poll_content=content["poll_content"],
                kind=kind,
                billed_to=api_key.owner,
                status=Message.Status.QUEUED,
            )
    except Exception:
        # The reservation is only released here, on the path where no Message
        # row was written. Once the row exists, releasing is the send task's
        # job and doing it here as well would refund twice.
        from apps.credits.services import release_credits

        release_credits(user_id=api_key.owner_id, reference=f"apikey:{api_key.pk}")
        logger.exception("Could not create an API message for key %s.", api_key.pk)
        raise PublicAPIError(MESSAGE_FAILED, "The message could not be queued.", status_code=500)

    from apps.messaging.tasks import send_message_task

    # Published after the transaction commits, for the reason campaign launch
    # documents: a worker can pick the task up before the row it names is
    # visible, and would then find nothing.
    transaction.on_commit(lambda: send_message_task.delay(message.pk))

    return message


def _message_kind(requested):
    """
    Which compliance class this send belongs to.

    Defaults to MARKETING because `Message.Kind` documents that a new send path
    should be protected by default and opt down rather than remember to opt in.
    `service` is available for what it is meant for -- an answer to something
    the contact just did, an OTP, an order update -- and it changes exactly one
    thing: quiet hours no longer hold it. It does **not** bypass an opt-out;
    nothing in this API can.
    """
    value = (requested or "").strip().lower()
    if value == "service":
        return Message.Kind.SERVICE
    if value in ("", "marketing"):
        return Message.Kind.MARKETING
    raise PublicAPIError(
        INVALID_REQUEST, "kind must be 'marketing' or 'service'.",
    )


# --- Campaigns -----------------------------------------------------------


@transaction.atomic
def create_campaign(api_key, data: dict):
    """
    Build a campaign from a list of recipients.

    The recipients are resolved into Contact rows and stored as a CONTACT_IDS
    audience -- the audience type the wizard's paste flow already uses. No new
    audience kind, because every downstream consumer (the dispatcher, the
    compliance gate, the report, the retry) understands that one and would have
    to grow a branch for another.

    Per-recipient `variables` are written to the contact's `custom_fields`,
    which is where the existing template renderer looks for personalization.
    That is what makes `{"name": "Aman"}` on an API recipient behave exactly
    like a column in an uploaded CSV.
    """
    if api_key.environment == ApiKey.Environment.TEST:
        raise PublicAPIError(
            TEST_KEY_CANNOT_SEND,
            "This is a test key. Create a live key (ss_live_...) to run real campaigns.",
            status_code=403,
        )

    account = resolve_account(api_key, data.get("account_id"))
    template = resolve_template(api_key, data["template_id"]) if data.get("template_id") else None

    contact_ids = []
    for entry in data["recipients"]:
        contact, _ = resolve_recipient(api_key, entry["phone"], name=entry.get("name", ""))
        variables = entry.get("variables") or {}
        if variables and contact is not None:
            # Merged, never replaced: a contact already carrying fields from a
            # CSV import must not lose them because one campaign named two.
            merged = {**(contact.custom_fields or {}), **{str(k): v for k, v in variables.items()}}
            if merged != contact.custom_fields:
                contact.custom_fields = merged
                contact.save(update_fields=["custom_fields", "updated_at"])
        if contact is not None:
            contact_ids.append(contact.pk)

    if not contact_ids:
        raise PublicAPIError(INVALID_RECIPIENT, "None of the recipients could be resolved to a contact.")

    message_type = PUBLIC_MESSAGE_TYPES[(data.get("type") or "text").lower()]
    campaign = Campaign.objects.create(
        tenant=api_key.tenant,
        created_by=api_key.owner,
        name=data["name"],
        whatsapp_account=account,
        template=template,
        message_type=message_type,
        message_body=data.get("text") or "",
        media_url=data.get("media_url") or "",
        caption=data.get("caption") or "",
        audience_type=Campaign.AudienceType.CONTACT_IDS,
        # De-duplicated while keeping the caller's order: a list with the same
        # number twice is a mistake that would otherwise message somebody twice.
        audience_contact_ids=list(dict.fromkeys(contact_ids)),
        scheduled_at=data.get("scheduled_at"),
        status=Campaign.Status.DRAFT,
    )
    return campaign


def campaign_transition(api_key, campaign, action: str):
    """
    Run one lifecycle transition through the existing campaign service.

    The states, the legality of each transition and the credit accounting are
    all `apps.campaigns.services`' business. This translates the verb and the
    exception, and nothing else.
    """
    from apps.campaigns import services as campaign_services
    from apps.campaigns.services import CampaignError

    verbs = {
        "start": campaign_services.launch,
        "pause": campaign_services.pause,
        "resume": campaign_services.resume,
        "cancel": campaign_services.cancel,
    }
    handler = verbs.get(action)
    if handler is None:
        raise PublicAPIError(INVALID_REQUEST, f"Unknown campaign action '{action}'.")

    try:
        return handler(campaign)
    except CampaignError as exc:
        # The campaign service raises one exception type for several different
        # refusals. Credit exhaustion is separated out because it is the one a
        # caller can act on programmatically -- top up and retry -- and would
        # otherwise be indistinguishable from "wrong state".
        message = str(exc)
        if "credit" in message.lower():
            raise PublicAPIError(INSUFFICIENT_CREDITS, message, status_code=402) from exc
        raise PublicAPIError(CAMPAIGN_STATE_INVALID, message, status_code=409) from exc
