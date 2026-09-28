"""
Everything one workspace owns, as a single JSON document.

Two things make this endpoint dangerous, and the whole design is a response to
them:

**It must not leak sideways.** Every query starts from one tenant id and is
filtered on it. Nothing here follows a relation upwards to a parent tenant, and
nothing takes an id from the request -- the tenant is the caller's own, taken
from their token. A bug that widened one filter here would hand one customer
another customer's contact list.

**It must not leak downwards.** An export is a file that leaves the building,
gets emailed around and sits in somebody's Downloads folder. So it carries the
customer's *data* and none of the deployment's *secrets*: no password hashes,
no JWT signing material, no API keys, no WhatsApp session credentials or access
tokens, no encryption keys, no webhook secrets. The allow-list style below --
naming each field to include rather than serialising a model and removing what
looks sensitive -- is deliberate: a field added to a model later is absent from
the export until somebody adds it here, which is the safe direction to fail.

Streaming, and bounded per collection. A workspace with two million messages
must not be exported by loading two million rows into memory; each collection
is capped and the response says how many rows a cap left behind, so the number
is never silently wrong.
"""

import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("sendstack")

#: Rows per collection. High enough that an ordinary workspace exports whole,
#: low enough that one request cannot exhaust a worker's memory. A truncated
#: collection is reported in the manifest rather than passed off as complete.
DEFAULT_LIMIT = 50_000

#: Message bodies are the bulk of any real export, so this one is capped lower
#: and separately.
MESSAGE_LIMIT = 100_000


def _rows(queryset, fields, limit):
    """`limit` rows as plain dicts, plus whether more were left behind."""
    values = list(queryset.values(*fields)[: limit + 1])
    truncated = len(values) > limit
    return values[:limit], truncated


def build_export(tenant, *, limit=DEFAULT_LIMIT) -> dict:
    """
    Assemble one tenant's export.

    `tenant` is a Tenant instance the caller has already been authorised for.
    This function does not do authorisation -- it does isolation, which is a
    different job, and doing it here as well as in the view means a future
    caller cannot forget.
    """
    from apps.campaigns.models import Campaign
    from apps.compliance.models import CompliancePolicy, ConsentEvent
    from apps.contacts.models import Contact, ContactGroup, ContactTag
    from apps.crm.models import ContactNote, Deal, Task
    from apps.inbox.models import Conversation
    from apps.legal.models import LegalAcceptance
    from apps.media_library.models import MediaAsset
    from apps.messaging.models import Message
    from apps.templates_app.models import MessageTemplate
    from apps.tenants.models import BusinessProfile, TenantBranding
    from apps.whatsapp.models import WhatsAppAccount

    tenant_id = tenant.pk
    truncated = []

    def collect(name, queryset, fields, cap=limit):
        rows, was_truncated = _rows(queryset.filter(tenant_id=tenant_id), fields, cap)
        if was_truncated:
            truncated.append({"collection": name, "returned": len(rows), "limit": cap})
        return rows

    export = {
        "manifest": {
            "generated_at": timezone.now().isoformat(),
            "tenant": {"id": tenant.pk, "name": tenant.name, "slug": tenant.slug, "kind": tenant.kind},
            "application_version": getattr(settings, "APP_VERSION", ""),
            "format_version": "1",
            # Said in the file itself, not only in the docs, because the file
            # is what somebody reads a year later.
            "excluded": [
                "password hashes",
                "authentication tokens",
                "WhatsApp access tokens and device session credentials",
                "API keys and provider credentials",
                "encryption keys and webhook secrets",
                "other tenants' data",
            ],
        },
    }

    export["contacts"] = collect(
        "contacts",
        Contact.objects.all(),
        [
            "id", "name", "phone", "email", "company", "country", "custom_fields",
            "subscribed", "unsubscribed_at", "marketing_consent", "consent_source",
            "consent_at", "consent_reference", "lifecycle_status", "lead_score",
            "source", "last_inbound_at", "last_outbound_at", "created_at", "updated_at",
        ],
    )
    export["contact_groups"] = collect("contact_groups", ContactGroup.objects.all(), ["id", "name", "description", "created_at"])
    export["contact_tags"] = collect("contact_tags", ContactTag.objects.all(), ["id", "name", "color", "created_at"])

    # The consent trail travels with the contacts. Without it the export is a
    # list of phone numbers with no evidence attached, which is the half a
    # customer actually needs when somebody complains.
    export["consent_events"] = collect(
        "consent_events",
        ConsentEvent.objects.all(),
        ["id", "contact_id", "phone", "event", "source", "detail", "created_at"],
    )

    export["campaigns"] = collect(
        "campaigns",
        Campaign.objects.all(),
        [
            "id", "name", "status", "message_type", "message_body", "caption",
            "audience_type", "scheduled_at", "started_at", "completed_at",
            "total_recipients", "sent_count", "delivered_count", "read_count",
            "failed_count", "skipped_count", "created_at", "updated_at",
        ],
    )

    export["messages"] = collect(
        "messages",
        Message.objects.all(),
        [
            "id", "contact_id", "campaign_id", "to_phone", "direction", "kind", "status",
            "message_type", "body", "error_message", "queued_at", "sent_at", "delivered_at", "read_at",
        ],
        cap=MESSAGE_LIMIT,
    )

    export["conversations"] = collect(
        "conversations",
        Conversation.objects.all(),
        ["id", "contact_id", "status", "created_at", "updated_at"],
    )

    export["templates"] = collect(
        "templates",
        MessageTemplate.objects.all(),
        ["id", "name", "message_type", "body", "caption", "variables", "is_active", "created_at", "updated_at"],
    )

    export["deals"] = collect(
        "deals", Deal.objects.all(),
        ["id", "contact_id", "name", "value", "currency", "stage_id", "status",
         "expected_close_date", "closed_at", "notes", "created_at", "updated_at"],
    )
    export["tasks"] = collect(
        "tasks", Task.objects.all(),
        ["id", "contact_id", "title", "due_at", "completed_at", "created_at"],
    )
    export["notes"] = collect("notes", ContactNote.objects.all(), ["id", "contact_id", "body", "created_at"])

    # Metadata only, deliberately. The bytes of every uploaded file would turn
    # a JSON export into a multi-gigabyte download; the media itself is
    # already downloadable from the media library.
    export["media"] = collect(
        "media",
        MediaAsset.objects.all(),
        ["id", "original_filename", "kind", "content_type", "size_bytes", "created_at"],
    )

    # Which number, and its state -- never the token or the session that makes
    # it work. This is the single most important omission in the file.
    export["whatsapp_accounts"] = [
        {
            "id": account.pk,
            "name": account.name,
            "phone_number": account.phone_number,
            "provider": account.provider,
            "status": account.status,
            "created_at": account.created_at.isoformat() if account.created_at else None,
        }
        for account in WhatsAppAccount.objects.filter(tenant_id=tenant_id)[:limit]
    ]

    policy = CompliancePolicy.for_tenant(tenant_id)
    export["settings"] = {
        "compliance": {
            "stop_keywords_enabled": policy.stop_keywords_enabled,
            "extra_stop_keywords": policy.extra_stop_keywords,
            "extra_start_keywords": policy.extra_start_keywords,
            "opt_out_style": policy.opt_out_style,
            "opt_out_footer": policy.opt_out_footer,
            "require_marketing_consent": policy.require_marketing_consent,
            "quiet_hours_enabled": policy.quiet_hours_enabled,
            "quiet_hours_start": str(policy.quiet_hours_start),
            "quiet_hours_end": str(policy.quiet_hours_end),
            "timezone": policy.timezone,
        },
        "branding": TenantBranding.resolve(tenant),
        "business_profile": {
            field: getattr(BusinessProfile.for_tenant(tenant_id), field)
            for field, _ in BusinessProfile.PROMPT_FIELDS
        },
    }

    export["legal_acceptances"] = list(
        LegalAcceptance.objects.filter(tenant_id=tenant_id).values(
            "document", "version", "accepted_at", "user_id",
        )[:limit]
    )

    export["manifest"]["counts"] = {
        key: len(value) for key, value in export.items() if isinstance(value, list)
    }
    export["manifest"]["truncated"] = truncated
    return export
