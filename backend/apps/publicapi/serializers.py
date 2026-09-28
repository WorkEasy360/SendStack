"""
The public representation of internal objects, and the request shapes.

Two jobs, and keeping them in one file is deliberate because they have to
agree: what goes out and what may come in are the same contract read from two
directions.

The guiding rule is that **the public API is a stable façade, not a view of the
schema.** Internal enum names (`QUEUED`, `QR_PROVIDER`, `CONTACT_IDS`), column
names and foreign keys are translated here into a vocabulary that can survive
the internals being refactored. Anything not translated here is not published.
"""

from django.utils import timezone
from rest_framework import serializers

from apps.campaigns.models import Campaign
from apps.messaging.models import Message
from apps.templates_app.models import MessageTemplate
from apps.whatsapp.models import WhatsAppAccount

from . import events as event_defs
from .models import ApiKey, WebhookDelivery, WebhookEndpoint
from .scopes import SCOPES

# --- Public identifiers --------------------------------------------------
#
# Prefixed ids, because `"msg_918"` in a customer's log is unambiguous where
# `918` is not, and because a prefixed id can be re-homed later (a uuid, a
# sharded id) without every integration having to change type.

MESSAGE_PREFIX = "msg_"
CAMPAIGN_PREFIX = "cmp_"
ACCOUNT_PREFIX = "account_"
TEMPLATE_PREFIX = "tpl_"
CONTACT_PREFIX = "cnt_"


def public_message_id(pk) -> str:
    return f"{MESSAGE_PREFIX}{pk}"


def parse_public_id(value, prefix) -> int | None:
    """
    Read a public id back to a primary key.

    The bare integer is accepted alongside the prefixed form, because half the
    integrations that will ever be written against this store ids as numbers
    and would otherwise fail on their own round trip.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.startswith(prefix):
        text = text[len(prefix):]
    try:
        parsed = int(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


# --- Vocabularies --------------------------------------------------------

#: Message.Status -> the seven statuses the API documents. SKIPPED collapses
#: into `failed` on purpose: the distinction between "the provider refused it"
#: and "we had nothing to send it from" is real and is preserved in
#: `failure_reason`, but an external caller's branch is the same either way,
#: and publishing an eighth status nobody expects breaks switch statements.
PUBLIC_STATUS = {
    Message.Status.QUEUED: "queued",
    Message.Status.SENDING: "processing",
    # RECOVERY_REQUIRED is an internal state -- the send's outcome is being
    # reconciled after a worker died mid-send. To an API consumer it is still
    # in flight, so it collapses into `processing` rather than adding an eighth
    # status nobody's integration branches on.
    Message.Status.RECOVERY_REQUIRED: "processing",
    Message.Status.SENT: "sent",
    Message.Status.DELIVERED: "delivered",
    Message.Status.READ: "read",
    Message.Status.FAILED: "failed",
    Message.Status.CANCELLED: "cancelled",
    Message.Status.SKIPPED: "failed",
}

#: WhatsAppAccount.Provider -> a name that describes the *integration*, not the
#: class that implements it.
PUBLIC_PROVIDER = {
    WhatsAppAccount.Provider.QR_PROVIDER: "qr_bridge",
    WhatsAppAccount.Provider.CLOUD_API: "cloud_api",
}

#: The message types the public API accepts, mapped to Message.MessageType.
#: `pdf` is listed separately from `document` because the brief asks for it and
#: because developers reach for it; it is the same thing on the wire.
PUBLIC_MESSAGE_TYPES = {
    "text": Message.MessageType.TEXT,
    "image": Message.MessageType.IMAGE,
    "video": Message.MessageType.VIDEO,
    "document": Message.MessageType.DOCUMENT,
    "pdf": Message.MessageType.DOCUMENT,
    "audio": Message.MessageType.DOCUMENT,
}

#: Types that must carry media, and types that must carry text.
MEDIA_TYPES = {"image", "video", "document", "pdf", "audio"}


# --- Outbound representations -------------------------------------------


def serialize_message(message) -> dict:
    return {
        "message_id": public_message_id(message.pk),
        "campaign_id": f"{CAMPAIGN_PREFIX}{message.campaign_id}" if message.campaign_id else None,
        "account_id": f"{ACCOUNT_PREFIX}{message.whatsapp_account_id}",
        "channel": "whatsapp",
        "direction": (message.direction or "").lower(),
        "recipient": message.to_phone,
        "type": (message.message_type or "TEXT").lower(),
        "text": message.body or "",
        "media_url": message.media_url or None,
        "caption": message.caption or None,
        "status": PUBLIC_STATUS.get(message.status, (message.status or "").lower()),
        "error": message.error_message or None,
        "failure_reason": message.failure_reason or None,
        "created_at": message.queued_at.isoformat() if message.queued_at else None,
        "updated_at": message.updated_at.isoformat() if message.updated_at else None,
        "sent_at": message.sent_at.isoformat() if message.sent_at else None,
        "delivered_at": message.delivered_at.isoformat() if message.delivered_at else None,
        "read_at": message.read_at.isoformat() if message.read_at else None,
    }


def serialize_account(account) -> dict:
    """
    A sending number, as an external integration may see it.

    Note what is *not* here and can never be added: `access_token_encrypted`,
    `app_secret_encrypted`, `webhook_verify_token`, `provider_session_id`, and
    anything the qr-bridge holds. This function is an allowlist written out by
    hand for exactly that reason -- a ModelSerializer with `exclude` would
    publish the next credential column somebody adds.
    """
    return {
        "id": f"{ACCOUNT_PREFIX}{account.pk}",
        "name": account.name,
        "channel": "whatsapp",
        "provider": PUBLIC_PROVIDER.get(account.provider, (account.provider or "").lower()),
        "status": (account.status or "").lower(),
        "phone": account.phone_number or None,
        "connected_at": account.last_connected_at.isoformat() if account.last_connected_at else None,
    }


def serialize_template(template) -> dict:
    return {
        "id": f"{TEMPLATE_PREFIX}{template.pk}",
        "name": template.name,
        "type": (template.message_type or "TEXT").lower(),
        "body": template.body or "",
        "caption": template.caption or None,
        "media_url": template.media_url or None,
        "variables": template.detected_variables(),
        "variable_defaults": template.variables or {},
        "is_active": template.is_active,
        "created_at": template.created_at.isoformat() if template.created_at else None,
        "updated_at": template.updated_at.isoformat() if template.updated_at else None,
    }


def serialize_campaign(campaign) -> dict:
    return {
        "id": f"{CAMPAIGN_PREFIX}{campaign.pk}",
        "name": campaign.name,
        "channel": "whatsapp",
        "account_id": f"{ACCOUNT_PREFIX}{campaign.whatsapp_account_id}",
        "template_id": f"{TEMPLATE_PREFIX}{campaign.template_id}" if campaign.template_id else None,
        "status": (campaign.status or "").lower(),
        "total_recipients": campaign.total_recipients,
        "sent_count": campaign.sent_count,
        "delivered_count": campaign.delivered_count,
        "read_count": campaign.read_count,
        "failed_count": campaign.failed_count,
        "skipped_count": campaign.skipped_count,
        "scheduled_at": campaign.scheduled_at.isoformat() if campaign.scheduled_at else None,
        "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
        "started_at": campaign.started_at.isoformat() if campaign.started_at else None,
        "completed_at": campaign.completed_at.isoformat() if campaign.completed_at else None,
    }


def serialize_contact(contact) -> dict:
    return {
        "id": f"{CONTACT_PREFIX}{contact.pk}",
        "phone": contact.phone,
        "name": contact.name or "",
        "email": getattr(contact, "email", "") or None,
        "company": getattr(contact, "company", "") or None,
        "subscribed": contact.subscribed,
        "marketing_consent": (contact.marketing_consent or "").lower() or None,
        "created_at": contact.created_at.isoformat() if getattr(contact, "created_at", None) else None,
    }


# --- Request shapes ------------------------------------------------------


class SendMessageSerializer(serializers.Serializer):
    """
    The body of ``POST /messages/send``.

    Validation here is about *shape*. Whether the account is connected, whether
    the workspace has credits, and whether this contact may be messaged at all
    are decided further down by the code that already owns those questions --
    see services.send_message and apps.messaging.gating.
    """

    channel = serializers.CharField(required=False, default="whatsapp")
    account_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    to = serializers.CharField()
    type = serializers.CharField(required=False, default="text")

    text = serializers.CharField(required=False, allow_blank=True, default="")
    media_url = serializers.CharField(required=False, allow_blank=True, default="")
    caption = serializers.CharField(required=False, allow_blank=True, default="")
    filename = serializers.CharField(required=False, allow_blank=True, default="")

    template_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    variables = serializers.DictField(required=False, default=dict)

    def validate_channel(self, value):
        # One channel is implemented. Named explicitly rather than ignored so
        # a caller asking for `sms` is told no rather than quietly sent a
        # WhatsApp message -- see the limitations note in the docs.
        if (value or "whatsapp").lower() != "whatsapp":
            raise serializers.ValidationError(
                "Only the 'whatsapp' channel is available on this deployment."
            )
        return "whatsapp"

    def validate_type(self, value):
        value = (value or "text").lower()
        if value not in PUBLIC_MESSAGE_TYPES:
            raise serializers.ValidationError(
                f"Unknown type '{value}'. Use one of: {', '.join(sorted(PUBLIC_MESSAGE_TYPES))}."
            )
        return value

    def validate(self, attrs):
        message_type = attrs.get("type", "text")
        has_template = bool(attrs.get("template_id"))

        if message_type in MEDIA_TYPES and not attrs.get("media_url") and not has_template:
            raise serializers.ValidationError(
                {"media_url": f"A '{message_type}' message needs a media_url."}
            )
        if message_type == "text" and not has_template and not (attrs.get("text") or "").strip():
            raise serializers.ValidationError(
                {"text": "A text message needs non-empty text, or a template_id."}
            )
        return attrs


class CampaignRecipientSerializer(serializers.Serializer):
    phone = serializers.CharField()
    name = serializers.CharField(required=False, allow_blank=True, default="")
    variables = serializers.DictField(required=False, default=dict)


class CreateCampaignSerializer(serializers.Serializer):
    """
    The body of ``POST /campaigns``.

    Recipients arrive as phone numbers with per-recipient variables. They are
    resolved into real Contact rows and the campaign is stored with the
    existing CONTACT_IDS audience type -- there is no new audience kind here.
    That is not a shortcut: consent, quiet hours, opt-out and the contact
    ceiling are all enforced against Contact rows, and an audience that bypassed
    them would be an audience that bypasses compliance.
    """

    name = serializers.CharField(max_length=150)
    channel = serializers.CharField(required=False, default="whatsapp")
    account_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    template_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    type = serializers.CharField(required=False, default="text")
    text = serializers.CharField(required=False, allow_blank=True, default="")
    media_url = serializers.CharField(required=False, allow_blank=True, default="")
    caption = serializers.CharField(required=False, allow_blank=True, default="")

    recipients = CampaignRecipientSerializer(many=True)
    scheduled_at = serializers.DateTimeField(required=False, allow_null=True)
    #: Off by default: creating a campaign and starting it are two API calls on
    #: purpose, so a caller that builds an audience wrongly finds out before it
    #: is sent rather than after. `start: true` is the shortcut for callers who
    #: have decided they do not need the check.
    start = serializers.BooleanField(required=False, default=False)

    def validate_channel(self, value):
        if (value or "whatsapp").lower() != "whatsapp":
            raise serializers.ValidationError(
                "Only the 'whatsapp' channel is available on this deployment."
            )
        return "whatsapp"

    def validate_recipients(self, value):
        if not value:
            raise serializers.ValidationError("A campaign needs at least one recipient.")
        limit = 10000
        if len(value) > limit:
            raise serializers.ValidationError(
                f"At most {limit} recipients per request. Split larger audiences across several campaigns."
            )
        return value

    def validate_scheduled_at(self, value):
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError("scheduled_at must be in the future.")
        return value

    def validate(self, attrs):
        if not attrs.get("template_id") and not (attrs.get("text") or "").strip() and not attrs.get("media_url"):
            raise serializers.ValidationError(
                "Provide a template_id, or inline text/media_url for the campaign to send."
            )
        return attrs


class TemplateWriteSerializer(serializers.ModelSerializer):
    """
    Create/update a template through the public API.

    A ModelSerializer here (unlike everywhere else in this file) because the
    write side genuinely is the internal model's fields, and because
    MessageTemplate's own uniqueness constraint is the thing that has to be
    enforced. The *read* side stays hand-written -- see `serialize_template`.
    """

    type = serializers.CharField(source="message_type", required=False)

    class Meta:
        model = MessageTemplate
        fields = ["name", "type", "body", "caption", "media_url", "filename", "variables", "is_active"]

    def validate_type(self, value):
        value = (value or "text").upper()
        if value not in MessageTemplate.MessageType.values:
            raise serializers.ValidationError(
                f"Unknown type. Use one of: {', '.join(v.lower() for v in MessageTemplate.MessageType.values)}."
            )
        return value

    def validate(self, attrs):
        message_type = attrs.get("message_type", MessageTemplate.MessageType.TEXT)
        if message_type != MessageTemplate.MessageType.TEXT and not attrs.get("media_url"):
            raise serializers.ValidationError({"media_url": "A media template needs a media_url."})
        if message_type == MessageTemplate.MessageType.TEXT and not (attrs.get("body") or "").strip():
            raise serializers.ValidationError({"body": "A text template needs a body."})
        return attrs


class WebhookEndpointSerializer(serializers.Serializer):
    """
    Create/update a webhook endpoint.

    `url` is checked against the SSRF guard before it is stored, not merely
    before it is called: an endpoint pointing at `169.254.169.254` is a
    credential-theft attempt, and refusing it at write time means it never
    exists to be retried, tested, or fired by a race.
    """

    name = serializers.CharField(required=False, allow_blank=True, max_length=100, default="")
    url = serializers.URLField(max_length=500)
    events = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    is_active = serializers.BooleanField(required=False, default=True)

    def validate_events(self, value):
        unknown = [event for event in value if event not in event_defs.EVENT_TYPES]
        if unknown:
            raise serializers.ValidationError(
                f"Unknown event(s): {', '.join(sorted(set(unknown)))}. "
                f"Valid events: {', '.join(event_defs.ALL_EVENT_TYPES)}."
            )
        return value

    def validate_url(self, value):
        from .webhook_urls import ensure_deliverable

        ensure_deliverable(value)
        return value


def serialize_webhook_endpoint(endpoint) -> dict:
    return {
        "id": endpoint.pk,
        "name": endpoint.name,
        "url": endpoint.url,
        "events": endpoint.events or list(event_defs.ALL_EVENT_TYPES),
        "is_active": endpoint.is_active,
        "last_success_at": endpoint.last_success_at.isoformat() if endpoint.last_success_at else None,
        "last_failure_at": endpoint.last_failure_at.isoformat() if endpoint.last_failure_at else None,
        "last_error": endpoint.last_error or None,
        "consecutive_failures": endpoint.consecutive_failures,
        "auto_disabled_at": endpoint.auto_disabled_at.isoformat() if endpoint.auto_disabled_at else None,
        "created_at": endpoint.created_at.isoformat() if endpoint.created_at else None,
    }


def serialize_delivery(delivery) -> dict:
    return {
        "id": delivery.pk,
        "endpoint_id": delivery.endpoint_id,
        "event": delivery.event_type,
        "event_id": delivery.event_id,
        "status": delivery.status.lower(),
        "attempts": delivery.attempt_count,
        "http_status": delivery.http_status,
        "response_ms": delivery.response_ms,
        "error": delivery.error or None,
        "created_at": delivery.created_at.isoformat() if delivery.created_at else None,
        "delivered_at": delivery.delivered_at.isoformat() if delivery.delivered_at else None,
        "next_attempt_at": delivery.next_attempt_at.isoformat() if delivery.next_attempt_at else None,
    }


# --- Dashboard (internal, JWT-authenticated) shapes ----------------------
#
# These serve Settings -> Developer in the SPA. They live here rather than in a
# second module because they publish the *same* objects and must not drift from
# the representations above.


class ApiKeyReadSerializer(serializers.ModelSerializer):
    masked_key = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    scope_descriptions = serializers.SerializerMethodField()
    allowed_account_ids = serializers.SerializerMethodField()

    class Meta:
        model = ApiKey
        fields = [
            "id", "name", "environment", "key_prefix", "masked_key", "scopes",
            "scope_descriptions", "allowed_account_ids", "status", "is_active",
            "requests_per_minute", "messages_per_minute", "campaigns_per_day",
            "expires_at", "revoked_at", "last_used_at", "last_used_ip",
            "last_user_agent", "request_count", "message_count",
            "rotated_at", "created_at",
        ]
        # Everything is read-only: creation mints a secret and must go through
        # services.create_api_key, and edits go through the explicit actions on
        # the viewset. A writable ModelSerializer here would be a way to change
        # `scopes` or `tenant` with a plain PATCH.
        read_only_fields = fields

    def get_scope_descriptions(self, obj):
        return {scope: SCOPES[scope] for scope in (obj.scopes or []) if scope in SCOPES}

    def get_allowed_account_ids(self, obj):
        return [account.pk for account in obj.allowed_accounts.all()]


class ApiKeyCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    scopes = serializers.ListField(child=serializers.CharField(), allow_empty=False)
    environment = serializers.ChoiceField(
        choices=ApiKey.Environment.choices, required=False, default=ApiKey.Environment.LIVE,
    )
    expires_at = serializers.DateTimeField(required=False, allow_null=True)
    allowed_account_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    requests_per_minute = serializers.IntegerField(required=False, min_value=0, default=0)
    messages_per_minute = serializers.IntegerField(required=False, min_value=0, default=0)
    campaigns_per_day = serializers.IntegerField(required=False, min_value=0, default=0)

    def validate_scopes(self, value):
        from .scopes import validate

        try:
            return validate(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate_expires_at(self, value):
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError("The expiry date must be in the future.")
        return value


class WebhookEndpointReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookEndpoint
        fields = [
            "id", "name", "url", "events", "is_active", "last_success_at",
            "last_failure_at", "last_error", "consecutive_failures",
            "auto_disabled_at", "created_at",
        ]
        read_only_fields = fields


class WebhookDeliveryReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookDelivery
        fields = [
            "id", "endpoint", "event_type", "event_id", "status", "attempt_count",
            "http_status", "response_ms", "response_snippet", "error",
            "created_at", "delivered_at", "next_attempt_at",
        ]
        read_only_fields = fields
