"""
The API request body for one template, generated from the template itself.

This is what the dashboard's **API JSON** panel shows and what the "Copy JSON"
and "Copy cURL" buttons put on the clipboard. It exists because the single
hardest thing about a templating API is knowing what to put in `variables`, and
the only correct answer is derived from the template's own text.

**The generated body is the real one.** Every field emitted here is a field
`SendMessageSerializer` declares and `services.send_message` reads -- there is a
test (`TemplateApiExampleTests.test_the_generated_body_is_accepted_by_the_real_serializer`)
that feeds this module's output
straight into that serializer and asserts it validates. That is the whole point
of generating rather than writing an example by hand: a documented body that
the backend would reject is worse than no example at all, and hand-written ones
drift the first time a field is renamed.

Two things are deliberately *not* emitted:

* **fields the backend does not support.** WhatsApp Cloud API's template
  vocabulary has `header`/`body`/`footer` component objects; this deployment's
  send contract does not, and inventing them so the JSON "looks like WhatsApp"
  would produce a body every request would 400 on. Media is expressed the way
  this API actually expresses it -- `type` plus `media_url`.
* **button and menu structure.** Buttons live on the template and are rendered
  server-side; the send call cannot override them, so listing them as request
  fields would suggest an ability that does not exist. They are reported
  separately, as `sends_with`, so the panel can still *say* what will go out.
"""

import json

from .keys import LIVE_PREFIX
from .openapi import server_url

#: What a variable with no example value is shown as. A type name rather than a
#: made-up value: `"name": "string"` tells a developer what to substitute,
#: while `"name": "Aman"` reads like something that will be sent.
PLACEHOLDER = "string"

#: The `type` a template's message_type maps to in the send body. Identical to
#: PUBLIC_MESSAGE_TYPES in serializers.py, inverted -- see `_public_type`.
_TYPE_BY_MODEL = {
    "TEXT": "text",
    "IMAGE": "image",
    "VIDEO": "video",
    "DOCUMENT": "document",
}


def _public_type(template) -> str:
    return _TYPE_BY_MODEL.get((template.message_type or "TEXT").upper(), "text")


def template_public_id(template) -> str:
    from .serializers import TEMPLATE_PREFIX

    return f"{TEMPLATE_PREFIX}{template.pk}"


def variable_example(template) -> dict:
    """
    ``{variable: example}`` for every placeholder the template actually uses.

    The template's own `variables` dict holds the fallback the renderer uses
    when a contact has no value for a field. Where one exists it is shown
    instead of the placeholder, because a real fallback is a better example
    than the word "string" and it is guaranteed to be a value this template was
    designed to receive.

    Order follows `detected_variables`, which reads body, caption, menu and
    button text in that order -- so the JSON reads in the order the variables
    appear in the message.
    """
    fallbacks = template.variables or {}
    example = {}
    for name in template.detected_variables():
        fallback = fallbacks.get(name)
        example[name] = str(fallback) if fallback not in (None, "") else PLACEHOLDER
    return example


def send_body(template, *, recipient="919876543210", account_id=None) -> dict:
    """
    The exact ``POST /messages/send`` body that sends this template.

    `to` and `account_id` are the two fields that cannot be derived from the
    template. `to` gets an obviously-placeholder number; `account_id` is
    omitted entirely rather than guessed, because omitting it is a *supported*
    and usually correct choice -- `services.resolve_account` picks the
    workspace's connected account when the field is absent.
    """
    body = {
        "to": recipient,
        "template_id": template_public_id(template),
    }
    if account_id:
        body["account_id"] = account_id

    variables = variable_example(template)
    if variables:
        body["variables"] = variables
    return body


def sends_with(template) -> dict:
    """
    What goes out that the request body does not control.

    Reported so the panel can be honest about the difference between "you send
    this" and "this is what arrives". A caller who wants different buttons
    edits the template; there is no request field for it.
    """
    detail = {"type": _public_type(template)}

    if template.media_url:
        detail["media"] = {
            "type": _public_type(template),
            "url": template.media_url,
            "filename": template.filename or None,
        }
    if template.caption:
        detail["caption"] = template.caption

    buttons = template.buttons or []
    if buttons:
        detail["buttons"] = [button.get("title", "") for button in buttons]

    list_content = template.list_content or {}
    if list_content.get("sections"):
        detail["menu"] = {
            "button": list_content.get("button", ""),
            "rows": sum(len(section.get("rows", [])) for section in list_content["sections"]),
        }

    poll_content = template.poll_content or {}
    if poll_content.get("options"):
        detail["poll"] = {
            "question": poll_content.get("question", ""),
            "options": list(poll_content["options"]),
        }
    return detail


def base_url(request=None) -> str:
    """
    The API base URL to print in an example, as a caller would really type it.

    One line, because there must be exactly one answer to this question across
    the docs page, the portal, the Postman export and every curl example. See
    `openapi.server_url` for the resolution order and why BACKEND_PUBLIC_URL is
    not part of it.
    """
    return server_url(request)


def curl_example(template, *, request=None, token=None) -> str:
    """
    A runnable curl command for this template.

    The token defaults to an obvious placeholder rather than a realistic-looking
    fake, so a copied command that was never edited fails at authentication
    with a clear message instead of looking plausible.
    """
    token = token or f"{LIVE_PREFIX}YOUR_API_KEY"
    payload = json.dumps(send_body(template), indent=2, ensure_ascii=False)
    # Indented to line up under the opening quote, so the pasted command reads
    # as one block in a terminal rather than a ragged left edge.
    payload = payload.replace("\n", "\n  ")
    quote = "'"
    return (
        f"curl -X POST {base_url(request)}/messages/send \\\n"
        f"  -H {quote}Authorization: Bearer {token}{quote} \\\n"
        f"  -H {quote}Content-Type: application/json{quote} \\\n"
        f"  -d {quote}{payload}{quote}"
    )


def describe(template, *, request=None) -> dict:
    """
    Everything the API JSON panel needs for one template, in one call.

    Assembled here rather than in the view so the dashboard endpoint, the
    public template detail endpoint and the tests all read the same function --
    which is what stops the panel and the API from describing different things.
    """
    variables = variable_example(template)
    return {
        "template_id": template_public_id(template),
        "template_name": template.name,
        "endpoint": {
            "method": "POST",
            "path": "/messages/send",
            "url": f"{base_url(request)}/messages/send",
            "required_scope": "messages:send",
        },
        "headers": {
            "Authorization": f"Bearer {LIVE_PREFIX}YOUR_API_KEY",
            "Content-Type": "application/json",
        },
        "request_body": send_body(template),
        "variables": [
            {
                "name": name,
                "type": "string",
                "example": value,
                # A variable whose example is not the placeholder came from the
                # template's own fallback, which means a caller that omits it
                # still gets something sensible. That is worth saying: it is
                # the difference between a required field and an optional one.
                "required": value == PLACEHOLDER,
            }
            for name, value in variables.items()
        ],
        "sends_with": sends_with(template),
        "curl": curl_example(template, request=request),
        "response_example": {
            "success": True,
            "message_id": "msg_123456",
            "status": "queued",
            "channel": "whatsapp",
            "account_id": "account_1",
            "recipient": "919876543210",
        },
        "notes": _notes(template),
    }


def _notes(template) -> list[str]:
    """
    The caveats that apply to *this* template, not to the API in general.

    Only the ones that would otherwise produce a surprise: a paused template is
    the commonest "why does my working integration suddenly 400?", and a
    template with no variables is worth saying out loud so nobody hunts for a
    `variables` field that was correctly omitted.
    """
    notes = []
    if not template.is_active:
        notes.append(
            "This template is paused. Sends referencing it are refused with INVALID_TEMPLATE "
            "until it is re-activated."
        )
    if not template.detected_variables():
        notes.append("This template has no variables, so the request body needs no `variables` field.")
    if template.media_url:
        notes.append(
            "The media on this template is sent automatically. You do not pass `media_url` "
            "when sending by `template_id`."
        )
    if template.buttons or (template.list_content or {}).get("sections") or (template.poll_content or {}).get("options"):
        notes.append(
            "Buttons, menus and polls come from the template and cannot be overridden per request. "
            "Edit the template to change them."
        )
    return notes
