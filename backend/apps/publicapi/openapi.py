"""
The OpenAPI 3.1 description of the public API.

Hand-written rather than generated. drf-spectacular already serves the SPA's
internal schema at ``/api/schema/`` and does an excellent job of describing
serializer-backed viewsets, but this API is deliberately not serializer-shaped:
its responses are hand-built envelopes, its ids are prefixed strings, and half
of what a developer needs (the error codes, the rate-limit headers, the
idempotency semantics, the webhook payloads) is not derivable from any Python
type. Generating it would produce a schema that is accurate about the fields
and silent about the contract.

Keeping it as data in one module means the docs page, the JSON endpoint and the
error-code table all read from the same source, so they cannot disagree.
"""

from django.conf import settings

from .errors import ERROR_CODES
from .events import (
    ALL_EVENT_TYPES,
    DELIVERY_HEADER,
    EVENT_HEADER,
    EVENT_TYPES,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
)
from .scopes import SCOPES

API_VERSION = "1.0.0"
BASE_PATH = "/api/public/v1"


def server_url(request=None) -> str:
    """
    The base URL printed in the docs, the spec and every generated example.

    Resolved in this order, and the order is the whole point:

    1. ``PUBLIC_API_BASE_URL``. A deployment that knows the address customers
       reach it on states it, and that beats anything we can infer -- notably
       when the API sits behind a different hostname from the dashboard.
    2. **the request**. The customer's browser just reached us on this host, so
       it is by construction an address that works from where they are. Safe to
       trust because Django has already validated it against ALLOWED_HOSTS.
    3. the bare path, for the callers that have no request (the OpenAPI
       document built for a test, say). Relative, but never wrong.

    ``BACKEND_PUBLIC_URL`` is deliberately *not* consulted. Under
    docker-compose it is ``http://backend:8000`` -- an internal Docker name --
    and printing that on the screen that answers "where do I take the URL
    from?" hands every customer an address that cannot work.
    """
    declared = (getattr(settings, "PUBLIC_API_BASE_URL", "") or "").rstrip("/")
    if declared:
        return f"{declared}{BASE_PATH}"
    if request is not None:
        return f"{request.scheme}://{request.get_host()}{BASE_PATH}"
    return BASE_PATH


def _envelope(properties: dict, required=None) -> dict:
    return {
        "type": "object",
        "properties": {"success": {"type": "boolean", "const": True}, **properties},
        "required": ["success"] + list(required or []),
    }


def _error_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "success": {"type": "boolean", "const": False},
            "error": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "enum": sorted(ERROR_CODES)},
                    "message": {"type": "string"},
                },
                "required": ["code", "message"],
            },
            "request_id": {"type": "string"},
        },
        "required": ["success", "error"],
    }


_MESSAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "message_id": {"type": "string", "examples": ["msg_918"]},
        "campaign_id": {"type": ["string", "null"], "examples": ["cmp_12"]},
        "account_id": {"type": "string", "examples": ["account_3"]},
        "channel": {"type": "string", "examples": ["whatsapp"]},
        "direction": {"type": "string", "enum": ["outbound", "inbound"]},
        "recipient": {"type": "string", "examples": ["+919999999999"]},
        "type": {"type": "string", "enum": ["text", "image", "video", "document"]},
        "text": {"type": "string"},
        "media_url": {"type": ["string", "null"]},
        "caption": {"type": ["string", "null"]},
        "status": {
            "type": "string",
            "enum": ["queued", "processing", "sent", "delivered", "read", "failed", "cancelled"],
        },
        "error": {"type": ["string", "null"]},
        "failure_reason": {"type": ["string", "null"]},
        "created_at": {"type": ["string", "null"], "format": "date-time"},
        "updated_at": {"type": ["string", "null"], "format": "date-time"},
        "sent_at": {"type": ["string", "null"], "format": "date-time"},
        "delivered_at": {"type": ["string", "null"], "format": "date-time"},
        "read_at": {"type": ["string", "null"], "format": "date-time"},
    },
}

_ACCOUNT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "examples": ["account_3"]},
        "name": {"type": "string"},
        "channel": {"type": "string", "examples": ["whatsapp"]},
        "provider": {"type": "string", "enum": ["qr_bridge", "cloud_api"]},
        "status": {"type": "string", "examples": ["connected"]},
        "phone": {"type": ["string", "null"]},
        "connected_at": {"type": ["string", "null"], "format": "date-time"},
    },
}

_TEMPLATE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "examples": ["tpl_7"]},
        "name": {"type": "string"},
        "type": {"type": "string"},
        "body": {"type": "string"},
        "caption": {"type": ["string", "null"]},
        "media_url": {"type": ["string", "null"]},
        "variables": {"type": "array", "items": {"type": "string"}},
        "variable_defaults": {"type": "object"},
        "is_active": {"type": "boolean"},
    },
}

_CAMPAIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "examples": ["cmp_12"]},
        "name": {"type": "string"},
        "channel": {"type": "string"},
        "account_id": {"type": "string"},
        "template_id": {"type": ["string", "null"]},
        "status": {
            "type": "string",
            "enum": ["draft", "scheduled", "queued", "running", "paused", "completed", "cancelled", "failed"],
        },
        "total_recipients": {"type": "integer"},
        "sent_count": {"type": "integer"},
        "delivered_count": {"type": "integer"},
        "read_count": {"type": "integer"},
        "failed_count": {"type": "integer"},
        "skipped_count": {"type": "integer"},
    },
}

_PAGINATION_SCHEMA = {
    "type": "object",
    "properties": {
        "total": {"type": "integer"},
        "limit": {"type": "integer"},
        "offset": {"type": "integer"},
        "has_more": {"type": "boolean"},
    },
}

_RATE_LIMIT_HEADERS = {
    "X-RateLimit-Limit": {"schema": {"type": "integer"}, "description": "Ceiling for the reported bucket."},
    "X-RateLimit-Remaining": {"schema": {"type": "integer"}, "description": "Calls left in the current window."},
    "X-RateLimit-Reset": {"schema": {"type": "integer"}, "description": "Unix time when the window resets."},
    "X-RateLimit-Bucket": {
        "schema": {"type": "string", "enum": ["requests_per_minute", "messages_per_minute", "campaigns_per_day"]},
        "description": "Which of the three buckets the numbers above describe.",
    },
}


def _responses(ok_schema, *, ok_status="200", ok_description="Success"):
    """Standard response block: one success, plus the failures every endpoint can return."""
    return {
        ok_status: {
            "description": ok_description,
            "headers": dict(_RATE_LIMIT_HEADERS),
            "content": {"application/json": {"schema": ok_schema}},
        },
        "400": {"description": "Invalid request", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
        "401": {"description": "Missing, invalid, expired or revoked API key", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
        "403": {"description": "The key lacks the required scope", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
        "404": {"description": "No such object in this workspace", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
        "429": {
            "description": "Rate limited",
            "headers": {**_RATE_LIMIT_HEADERS, "Retry-After": {"schema": {"type": "integer"}}},
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
        },
    }


_IDEMPOTENCY_PARAM = {
    "name": "Idempotency-Key",
    "in": "header",
    "required": False,
    "schema": {"type": "string", "maxLength": 255},
    "description": (
        "A unique id for this operation. Retrying with the same key and the same body "
        "returns the stored response instead of acting again."
    ),
}

_PAGE_PARAMS = [
    {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50, "maximum": 200}},
    {"name": "offset", "in": "query", "schema": {"type": "integer", "default": 0}},
]


def build_spec(request=None) -> dict:
    """
    The complete OpenAPI document.

    `request` is threaded through only so `servers[0].url` is the address the
    reader actually reached us on -- a spec whose server URL is wrong generates
    a client that cannot connect, which is the one field in here that has to be
    right for the document to be usable at all.
    """
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "SendStack Messaging API",
            "version": API_VERSION,
            "summary": "Send WhatsApp messages, run campaigns and receive events from any application.",
            "description": (
                "Authenticate with `Authorization: Bearer ss_live_...`. Every response carries "
                "`success`; failures carry `error.code` from a fixed vocabulary. Sends are "
                "asynchronous -- `POST /messages/send` returns 202 with a `message_id` you can "
                "poll or subscribe to."
            ),
        },
        "servers": [{"url": server_url(request)}],
        "security": [{"ApiKeyAuth": []}],
        "tags": [
            {"name": "Authentication", "description": "Confirm a key works before building anything on it."},
            {"name": "Messages", "description": "Send a message and track what happened to it."},
            {"name": "Campaigns", "description": "Bulk sends with a lifecycle."},
            {"name": "Templates", "description": "Reusable message content with variables."},
            {"name": "Contacts", "description": "The people you message."},
            {"name": "Accounts", "description": "The WhatsApp numbers available to send from."},
            {"name": "Media", "description": "Upload attachments."},
            {"name": "Reports", "description": "Aggregate delivery numbers."},
            {"name": "Webhooks", "description": "Receive events instead of polling."},
        ],
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": (
                        "Your API key, e.g. `ss_live_7f3a91c24b8e0d5a91c37e6f2b840195cd7ae3f1`. "
                        "Create one in Settings -> Developer -> API Keys. The full key is shown once."
                    ),
                }
            },
            "schemas": {
                "Error": _error_schema(),
                "Message": _MESSAGE_SCHEMA,
                "Account": _ACCOUNT_SCHEMA,
                "Template": _TEMPLATE_SCHEMA,
                "Campaign": _CAMPAIGN_SCHEMA,
                "Pagination": _PAGINATION_SCHEMA,
            },
        },
        "x-scopes": [{"name": name, "description": description} for name, description in SCOPES.items()],
        "x-error-codes": [{"code": code, "description": description} for code, description in sorted(ERROR_CODES.items())],
        "x-webhook-events": [{"event": name, "description": description} for name, description in EVENT_TYPES.items()],
        "x-webhook-headers": {
            "signature": SIGNATURE_HEADER,
            "timestamp": TIMESTAMP_HEADER,
            "event": EVENT_HEADER,
            "delivery": DELIVERY_HEADER,
        },
        "webhooks": _webhooks(),
        "paths": _paths(),
    }


def _webhooks() -> dict:
    """The OpenAPI 3.1 `webhooks` block -- what we POST to the subscriber."""
    return {
        event: {
            "post": {
                "summary": EVENT_TYPES[event],
                "description": (
                    f"Sent to every active endpoint subscribed to `{event}`. "
                    f"Verify `{SIGNATURE_HEADER}` before trusting the body."
                ),
                "parameters": [
                    {"name": SIGNATURE_HEADER, "in": "header", "required": True,
                     "schema": {"type": "string"}, "description": "sha256=<hex HMAC of '<timestamp>.<raw body>'>"},
                    {"name": TIMESTAMP_HEADER, "in": "header", "required": True,
                     "schema": {"type": "integer"}, "description": "Unix seconds. Reject if more than 5 minutes old."},
                    {"name": EVENT_HEADER, "in": "header", "required": True, "schema": {"type": "string"}},
                    {"name": DELIVERY_HEADER, "in": "header", "required": True,
                     "schema": {"type": "string"}, "description": "Stable across retries. Deduplicate on it."},
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string", "examples": ["evt_9c1f..."]},
                                    "event": {"type": "string", "const": event},
                                    "timestamp": {"type": "string", "format": "date-time"},
                                    "data": {"type": "object"},
                                },
                            }
                        }
                    }
                },
                "responses": {"2XX": {"description": "Acknowledged. Any 2xx counts; answer fast and process async."}},
            }
        }
        for event in ALL_EVENT_TYPES
    }


def _paths() -> dict:
    return {
        "/messages/send": {
            "post": {
                "tags": ["Messages"],
                "summary": "Send a message",
                "description": (
                    "Queues one message. Returns 202 -- accepted, not delivered. "
                    "Requires the `messages:send` scope."
                ),
                "parameters": [_IDEMPOTENCY_PARAM],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["to"],
                                "properties": {
                                    "channel": {"type": "string", "default": "whatsapp"},
                                    "account_id": {"type": "string", "description": "Omit to let SendStack choose a connected number."},
                                    "to": {"type": "string", "examples": ["919999999999"]},
                                    "type": {"type": "string", "enum": ["text", "image", "video", "document", "pdf", "audio"], "default": "text"},
                                    "text": {"type": "string"},
                                    "media_url": {"type": "string"},
                                    "caption": {"type": "string"},
                                    "filename": {"type": "string"},
                                    "template_id": {"type": "string", "description": "A template id or its name."},
                                    "variables": {"type": "object", "additionalProperties": {"type": "string"}},
                                    "kind": {
                                        "type": "string", "enum": ["marketing", "service"], "default": "marketing",
                                        "description": "`service` is exempt from quiet hours. Neither bypasses an opt-out.",
                                    },
                                },
                            },
                            "examples": {
                                "text": {"summary": "Plain text", "value": {
                                    "channel": "whatsapp", "account_id": "account_3",
                                    "to": "919999999999", "type": "text",
                                    "text": "Hello from my application",
                                }},
                                "image": {"summary": "Image with a caption", "value": {
                                    "channel": "whatsapp", "to": "919999999999", "type": "image",
                                    "media_url": "https://example.com/receipt.png", "caption": "Your receipt",
                                }},
                                "template": {"summary": "Template with variables", "value": {
                                    "channel": "whatsapp", "to": "919999999999",
                                    "template_id": "welcome_customer",
                                    "variables": {"name": "Aman", "order_id": "ORD123"},
                                }},
                            },
                        }
                    },
                },
                "responses": _responses(
                    _envelope({
                        "message_id": {"type": "string", "examples": ["msg_918"]},
                        "status": {"type": "string", "const": "queued"},
                        "channel": {"type": "string"},
                        "account_id": {"type": "string"},
                        "recipient": {"type": "string"},
                    }, required=["message_id", "status"]),
                    ok_status="202",
                    ok_description="Accepted and queued.",
                ),
            }
        },
        "/messages": {
            "get": {
                "tags": ["Messages"], "summary": "List messages",
                "description": "Requires `messages:read`.",
                "parameters": _PAGE_PARAMS + [
                    {"name": "status", "in": "query", "schema": {"type": "string"}},
                    {"name": "direction", "in": "query", "schema": {"type": "string", "enum": ["inbound", "outbound"]}},
                    {"name": "account_id", "in": "query", "schema": {"type": "string"}},
                    {"name": "recipient", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": _responses(_envelope({
                    "data": {"type": "array", "items": {"$ref": "#/components/schemas/Message"}},
                    "pagination": {"$ref": "#/components/schemas/Pagination"},
                })),
            }
        },
        "/messages/{message_id}": {
            "get": {
                "tags": ["Messages"], "summary": "Get message status",
                "description": "Requires `messages:status`.",
                "parameters": [{"name": "message_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": _responses(_envelope({"message_id": {"type": "string"}, "status": {"type": "string"}})),
            }
        },
        "/campaigns": {
            "get": {
                "tags": ["Campaigns"], "summary": "List campaigns",
                "description": "Requires `campaigns:read`.",
                "parameters": _PAGE_PARAMS + [{"name": "status", "in": "query", "schema": {"type": "string"}}],
                "responses": _responses(_envelope({
                    "data": {"type": "array", "items": {"$ref": "#/components/schemas/Campaign"}},
                    "pagination": {"$ref": "#/components/schemas/Pagination"},
                })),
            },
            "post": {
                "tags": ["Campaigns"], "summary": "Create a campaign",
                "description": "Requires `campaigns:create`. Add `\"start\": true` to launch immediately (also needs `campaigns:send`).",
                "parameters": [_IDEMPOTENCY_PARAM],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["name", "recipients"],
                            "properties": {
                                "name": {"type": "string"},
                                "channel": {"type": "string", "default": "whatsapp"},
                                "account_id": {"type": "string"},
                                "template_id": {"type": "string"},
                                "type": {"type": "string", "default": "text"},
                                "text": {"type": "string"},
                                "media_url": {"type": "string"},
                                "recipients": {
                                    "type": "array", "maxItems": 10000,
                                    "items": {
                                        "type": "object", "required": ["phone"],
                                        "properties": {
                                            "phone": {"type": "string"},
                                            "name": {"type": "string"},
                                            "variables": {"type": "object"},
                                        },
                                    },
                                },
                                "scheduled_at": {"type": "string", "format": "date-time"},
                                "start": {"type": "boolean", "default": False},
                            },
                        },
                        # Inline text first, deliberately: it is the example
                        # somebody can run without first owning a template
                        # called "promo_template", and the generated Postman
                        # collection takes whichever comes first.
                        "examples": {
                            "inline": {"summary": "Inline text campaign", "value": {
                                "name": "January Promotion", "channel": "whatsapp",
                                "type": "text",
                                "text": "Hi [name], our January sale starts today.",
                                "recipients": [{"phone": "919999999999", "name": "Aman", "variables": {"name": "Aman"}}],
                            }},
                            "template": {"summary": "Template campaign", "value": {
                                "name": "January Promotion", "channel": "whatsapp",
                                "account_id": "account_3", "template_id": "tmpl_12",
                                "recipients": [{"phone": "919999999999", "variables": {"name": "Aman"}}],
                            }},
                        },
                    }},
                },
                "responses": _responses(
                    _envelope({"id": {"type": "string"}, "status": {"type": "string"}}),
                    ok_status="201", ok_description="Created.",
                ),
            },
        },
        "/campaigns/{campaign_id}": {
            "get": {
                "tags": ["Campaigns"], "summary": "Get a campaign",
                "description": "Requires `campaigns:read`.",
                "parameters": [{"name": "campaign_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": _responses(_envelope({"id": {"type": "string"}})),
            }
        },
        "/campaigns/{campaign_id}/results": {
            "get": {
                "tags": ["Campaigns"], "summary": "Per-recipient results",
                "description": "Requires `campaigns:read`.",
                "parameters": [{"name": "campaign_id", "in": "path", "required": True, "schema": {"type": "string"}}] + _PAGE_PARAMS,
                "responses": _responses(_envelope({
                    "data": {"type": "array", "items": {"$ref": "#/components/schemas/Message"}},
                    "campaign": {"$ref": "#/components/schemas/Campaign"},
                })),
            }
        },
        **{
            f"/campaigns/{{campaign_id}}/{action}": {
                "post": {
                    "tags": ["Campaigns"],
                    "summary": f"{action.capitalize()} a campaign",
                    "description": f"Requires `{'campaigns:cancel' if action == 'cancel' else 'campaigns:send'}`.",
                    "parameters": [{"name": "campaign_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": _responses(_envelope({"id": {"type": "string"}, "status": {"type": "string"}})),
                }
            }
            for action in ("start", "pause", "resume", "cancel")
        },
        "/templates": {
            "get": {
                "tags": ["Templates"], "summary": "List templates",
                "description": "Requires `templates:read`.",
                "parameters": _PAGE_PARAMS + [
                    {"name": "active", "in": "query", "schema": {"type": "boolean"}},
                    {"name": "search", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": _responses(_envelope({
                    "data": {"type": "array", "items": {"$ref": "#/components/schemas/Template"}},
                    "pagination": {"$ref": "#/components/schemas/Pagination"},
                })),
            },
            "post": {
                "tags": ["Templates"], "summary": "Create a template",
                "description": "Requires `templates:write`.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string", "default": "text"},
                        "body": {"type": "string", "description": "Use [Name] or {{name}} for variables."},
                        "caption": {"type": "string"},
                        "media_url": {"type": "string"},
                        "variables": {"type": "object", "description": "Fallback values per variable."},
                        "is_active": {"type": "boolean", "default": True},
                    },
                },
                # An explicit example, because this body has an interdependency
                # a schema cannot express: a text template needs a non-empty
                # `body`, and a media one needs a `media_url`. A synthesised
                # example would satisfy the schema and be rejected by the view.
                # `[name]` rather than the `{{name}}` form, which is equally
                # valid here but is *also* Postman's own variable syntax --
                # Postman would substitute it away before sending, and the
                # template would be created with the placeholders blanked out.
                "example": {
                    "name": "order_confirmation",
                    "type": "text",
                    "body": "Hello [name], your order [order_id] has been confirmed.",
                    "variables": {"name": "there"},
                    "is_active": True,
                }}}},
                "responses": _responses(_envelope({"id": {"type": "string"}}), ok_status="201", ok_description="Created."),
            },
        },
        "/templates/{template_id}": {
            "get": {
                "tags": ["Templates"], "summary": "Get a template",
                "description": "Requires `templates:read`. Accepts an id or a name.",
                "parameters": [{"name": "template_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": _responses(_envelope({"id": {"type": "string"}})),
            },
            "put": {
                "tags": ["Templates"], "summary": "Update a template",
                "description": "Requires `templates:write`.",
                "parameters": [{"name": "template_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": _responses(_envelope({"id": {"type": "string"}})),
            },
            "delete": {
                "tags": ["Templates"], "summary": "Delete a template",
                "description": "Requires `templates:write`. Refused with 409 if a campaign uses it -- deactivate instead.",
                "parameters": [{"name": "template_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": _responses(_envelope({"deleted": {"type": "boolean"}})),
            },
        },
        "/contacts": {
            "get": {
                "tags": ["Contacts"], "summary": "List contacts",
                "description": "Requires `contacts:read`.",
                "parameters": _PAGE_PARAMS + [
                    {"name": "phone", "in": "query", "schema": {"type": "string"}},
                    {"name": "search", "in": "query", "schema": {"type": "string"}},
                    {"name": "subscribed", "in": "query", "schema": {"type": "boolean"}},
                ],
                "responses": _responses(_envelope({"data": {"type": "array", "items": {"type": "object"}}})),
            },
            "post": {
                "tags": ["Contacts"], "summary": "Create or update a contact",
                "description": (
                    "Requires `contacts:write`. Upserts: an existing contact has blank fields "
                    "filled in and is never overwritten. `outcome` says which happened."
                ),
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["phone"],
                    "properties": {
                        "phone": {"type": "string"}, "name": {"type": "string"},
                        "email": {"type": "string"}, "company": {"type": "string"},
                        "custom_fields": {"type": "object"},
                    },
                },
                "example": {
                    "phone": "919876543210",
                    "name": "Aman Gupta",
                    "email": "aman@example.com",
                    "company": "Acme Ltd",
                    "custom_fields": {"city": "Delhi"},
                }}}},
                "responses": _responses(_envelope({"id": {"type": "string"}, "outcome": {"type": "string", "enum": ["created", "updated", "unchanged"]}})),
            },
        },
        "/contacts/{contact_id}": {
            "get": {
                "tags": ["Contacts"], "summary": "Get a contact",
                "description": "Requires `contacts:read`.",
                "parameters": [{"name": "contact_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": _responses(_envelope({"id": {"type": "string"}})),
            }
        },
        "/accounts": {
            "get": {
                "tags": ["Accounts"], "summary": "List sending accounts",
                "description": (
                    "Requires `accounts:read`. Never exposes provider credentials, session data "
                    "or authentication state of any kind."
                ),
                "parameters": [{"name": "status", "in": "query", "schema": {"type": "string"}}],
                "responses": _responses(_envelope({
                    "accounts": {"type": "array", "items": {"$ref": "#/components/schemas/Account"}},
                })),
            }
        },
        "/accounts/{account_id}": {
            "get": {
                "tags": ["Accounts"], "summary": "Get one account",
                "description": "Requires `accounts:read`.",
                "parameters": [{"name": "account_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": _responses(_envelope({"id": {"type": "string"}})),
            }
        },
        "/media": {
            "post": {
                "tags": ["Media"], "summary": "Upload media",
                "description": (
                    "Requires `media:upload`. multipart/form-data with a `file` field. "
                    "Returns an absolute `url` to pass as `media_url`."
                ),
                "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                    "type": "object", "properties": {"file": {"type": "string", "format": "binary"}},
                }}}},
                "responses": _responses(_envelope({"url": {"type": "string"}, "kind": {"type": "string"}}), ok_status="201", ok_description="Uploaded."),
            }
        },
        "/webhooks": {
            "get": {
                "tags": ["Webhooks"], "summary": "List endpoints",
                "description": "Requires `webhooks:read`.",
                "parameters": _PAGE_PARAMS,
                "responses": _responses(_envelope({"data": {"type": "array", "items": {"type": "object"}}})),
            },
            "post": {
                "tags": ["Webhooks"], "summary": "Register an endpoint",
                "description": "Requires `webhooks:manage`. The signing `secret` is returned once and never again.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["url"],
                    "properties": {
                        "name": {"type": "string"},
                        "url": {"type": "string", "format": "uri", "description": "https required in production."},
                        "events": {"type": "array", "items": {"type": "string", "enum": list(ALL_EVENT_TYPES)},
                                   "description": "Empty means every event."},
                        "is_active": {"type": "boolean", "default": True},
                    },
                },
                "example": {
                    "name": "Order service",
                    "url": "https://example.com/hooks/sendstack",
                    "events": ["message.delivered", "message.failed"],
                    "is_active": True,
                }}}},
                "responses": _responses(_envelope({"id": {"type": "integer"}, "secret": {"type": "string"}}), ok_status="201", ok_description="Created."),
            },
        },
        "/webhooks/{endpoint_id}": {
            "get": {"tags": ["Webhooks"], "summary": "Get an endpoint", "description": "Requires `webhooks:read`.",
                    "parameters": [{"name": "endpoint_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                    "responses": _responses(_envelope({"id": {"type": "integer"}}))},
            "patch": {"tags": ["Webhooks"], "summary": "Update an endpoint", "description": "Requires `webhooks:manage`.",
                      "parameters": [{"name": "endpoint_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                      "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                      "responses": _responses(_envelope({"id": {"type": "integer"}}))},
            "delete": {"tags": ["Webhooks"], "summary": "Delete an endpoint", "description": "Requires `webhooks:manage`.",
                       "parameters": [{"name": "endpoint_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                       "responses": _responses(_envelope({"deleted": {"type": "boolean"}}))},
        },
        "/webhooks/{endpoint_id}/test": {
            "post": {"tags": ["Webhooks"], "summary": "Send a test delivery",
                     "description": "Requires `webhooks:manage`. Goes through the real delivery path, signature included.",
                     "parameters": [{"name": "endpoint_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                     "responses": _responses(_envelope({"delivery": {"type": "object"}}), ok_status="202", ok_description="Queued.")},
        },
        "/webhooks/{endpoint_id}/rotate-secret": {
            "post": {"tags": ["Webhooks"], "summary": "Rotate the signing secret",
                     "description": "Requires `webhooks:manage`. The old secret stops working immediately.",
                     "parameters": [{"name": "endpoint_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                     "responses": _responses(_envelope({"secret": {"type": "string"}}))},
        },
        "/webhooks/deliveries": {
            "get": {"tags": ["Webhooks"], "summary": "Delivery log", "description": "Requires `webhooks:read`.",
                    "parameters": _PAGE_PARAMS + [
                        {"name": "endpoint_id", "in": "query", "schema": {"type": "integer"}},
                        {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["pending", "succeeded", "failed"]}},
                    ],
                    "responses": _responses(_envelope({"data": {"type": "array", "items": {"type": "object"}}}))},
        },
        "/reports/summary": {
            "get": {
                "tags": ["Reports"], "summary": "Headline delivery numbers",
                "description": "Requires `reports:read`. Omit `range` for all time.",
                "parameters": [
                    {"name": "range", "in": "query",
                     "schema": {"type": "string", "enum": ["today", "yesterday", "last_7_days", "last_30_days", "custom"]}},
                    {"name": "start_date", "in": "query", "schema": {"type": "string", "format": "date"},
                     "description": "With range=custom."},
                    {"name": "end_date", "in": "query", "schema": {"type": "string", "format": "date"},
                     "description": "With range=custom. Inclusive."},
                ],
                "responses": _responses(_envelope({
                    "messages": {"type": "object"},
                    "rates": {"type": "object"},
                    "campaigns": {"type": "object"},
                    "accounts": {"type": "object"},
                })),
            }
        },
        "/reports/daily": {
            "get": {
                "tags": ["Reports"], "summary": "Per-day message breakdown",
                "description": "Requires `reports:read`. One row per day, for charting.",
                "parameters": [
                    {"name": "range", "in": "query",
                     "schema": {"type": "string", "enum": ["today", "yesterday", "last_7_days", "last_30_days", "custom"]}},
                ],
                "responses": _responses(_envelope({"days": {"type": "array", "items": {"type": "object"}}})),
            }
        },
        "/health": {
            "get": {
                "tags": ["Authentication"],
                "summary": "Test authentication",
                "description": (
                    "Confirms the API is reachable and, when an `Authorization` header is present, "
                    "that the key is valid. Works without a credential -- it answers "
                    "`authenticated: false` rather than 401, so it doubles as an uptime check. "
                    "An *invalid* key is still a 401. Requires no scope."
                ),
                # `[]` means "security is optional here" in OpenAPI: the empty
                # requirement is satisfied by sending nothing, and the named one
                # by sending a key. Both are genuinely accepted.
                "security": [{}, {"ApiKeyAuth": []}],
                "responses": _responses(_envelope({
                    "api": {"type": "string", "const": "online"},
                    "version": {"type": "string", "const": "v1"},
                    "authenticated": {"type": "boolean"},
                    "timestamp": {"type": "string", "format": "date-time"},
                    "user": {"type": "object", "properties": {
                        "id": {"type": "string"}, "email": {"type": "string"},
                    }},
                    "workspace": {"type": "object", "properties": {
                        "id": {"type": "integer"}, "name": {"type": "string"},
                    }},
                    "token": {"type": "object", "properties": {
                        "name": {"type": "string"},
                        "key": {"type": "string", "description": "Masked. The full key is never returned."},
                        "environment": {"type": "string", "enum": ["live", "test"]},
                        "scopes": {"type": "array", "items": {"type": "string"}},
                        "permissions": {"type": "array", "items": {"type": "string"},
                                        "description": "The same scopes in plain English."},
                    }},
                })),
            }
        },
        "/diagnostics": {
            "get": {
                "tags": ["Authentication"],
                "summary": "Check everything needed to send",
                "description": (
                    "Walks the whole chain a send depends on -- token, permission, workspace, "
                    "WhatsApp connection, credits -- and reports each link separately. Use it to "
                    "tell 'my key is wrong' apart from 'my WhatsApp number is disconnected'. "
                    "Sends nothing and reserves nothing. Requires no scope."
                ),
                "responses": _responses(_envelope({
                    "ready_to_send": {"type": "boolean"},
                    "summary": {"type": "string"},
                    "checks": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "enum": [
                                "authentication", "permissions", "workspace",
                                "whatsapp_connection", "credits",
                            ]},
                            "label": {"type": "string"},
                            "status": {"type": "string", "enum": ["pass", "fail"]},
                            "detail": {"type": "string"},
                        },
                    }},
                })),
            }
        },
        "/ping": {
            "get": {
                "tags": ["Authentication"], "summary": "Verify a key (alias of /health)",
                "description": (
                    "Confirms the key works and reports what it can do. Requires no scope -- it is "
                    "what an integration calls during setup, before it knows whether the customer "
                    "ticked the right boxes."
                ),
                "responses": _responses(_envelope({
                    "workspace": {"type": "string"},
                    "environment": {"type": "string", "enum": ["live", "test"]},
                    "scopes": {"type": "array", "items": {"type": "string"}},
                })),
            }
        },
    }
