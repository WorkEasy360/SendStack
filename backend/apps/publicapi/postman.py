"""
A Postman collection and environment, generated from the OpenAPI document.

**Generated, not written.** The collection is built by walking `build_spec()`,
so every request in it is an endpoint that exists, at the path it is really
mounted on, with the scope the view really requires. A hand-maintained
collection would be correct on the day it was written and wrong by the second
refactor -- and a customer whose first experience of the API is a 404 from the
collection we gave them does not try a third time.

The trade is that the collection inherits the spec's shape rather than being
curated: request order follows the spec's tag order, and an endpoint the spec
does not describe does not appear. That is the right way round. A missing
endpoint is a gap in the spec worth fixing; an endpoint in the collection that
the API does not serve is a lie we shipped.

Two files come out of here, because Postman wants them separately:

* the **collection** -- folders of requests, using ``{{base_url}}`` and
  ``{{api_token}}`` everywhere and never a literal;
* the **environment** -- the values for those variables. The token is exported
  as a Postman *secret* type so it is masked in the UI, and it is exported
  empty unless the caller explicitly passes one. Baking a live key into a file
  people commit to their repository is not a convenience worth having.
"""

import json

from .openapi import API_VERSION, build_spec

#: The variables the brief names, plus the two that make the message examples
#: runnable without editing three requests by hand.
ENVIRONMENT_VARIABLES = [
    ("base_url", "", "The API base URL, ending in /api/public/v1"),
    ("api_token", "", "Your API key from Settings -> Developer -> API Keys"),
    ("phone", "919876543210", "A recipient in international format, digits only"),
    ("template_name", "", "The id of a template, e.g. tmpl_12"),
    ("account_id", "", "Optional: which WhatsApp number to send from, e.g. account_1"),
]

#: Which spec tag becomes which folder, in the order a developer meets them.
#: Anything tagged with something not listed here lands in "Other" rather than
#: being dropped -- a new tag should show up looking untidy, not vanish.
_FOLDER_ORDER = [
    "Authentication",
    "Messages",
    "Templates",
    "Contacts",
    "Campaigns",
    "Accounts",
    "Media",
    "Reports",
    "Webhooks",
    "Other",
]

#: Spec paths whose examples read better with an environment variable than with
#: the literal the spec carries. Keyed by path so a renamed field cannot
#: silently stop the substitution -- the path either matches or it does not.
_BODY_SUBSTITUTIONS = {
    "/messages/send": {"to": "{{phone}}", "template_id": "{{template_name}}"},
}

#: Fields dropped from an example because the spec's illustrative value would
#: not work in the reader's workspace. `account_id: "account_3"` reads fine in
#: documentation and 404s for everybody who imports it, and omitting it is a
#: supported choice that makes the request work as-is -- see
#: services.resolve_account.
_BODY_OMISSIONS = {
    "/messages/send": ("account_id",),
}


def build_collection(*, base_url: str = "", name: str = "WhatsApp Marketing API", request=None) -> dict:
    """
    The Postman v2.1 collection.

    `base_url` is only used for the collection-level variable default; every
    request still references ``{{base_url}}``, so importing the collection into
    an environment that overrides it works exactly as a Postman user expects.
    """
    spec = build_spec(request)
    folders = {tag: [] for tag in _FOLDER_ORDER}

    # The one request that is not in the spec's `paths` as something a
    # developer thinks of as an endpoint: "does my token work?". It is
    # /health, and it goes first because it is step one of the quick start.
    folders["Authentication"] = [
        _request(
            name="Test authentication",
            method="GET",
            path="/health",
            description=(
                "Confirms the API is reachable and your token is valid. "
                "Returns authenticated: true and the workspace the token belongs to. "
                "Run this first -- if it fails, nothing else will work."
            ),
            auth=True,
        )
    ]

    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            # /health and /ping are already in Authentication above, and the
            # doc endpoints are pages rather than API calls.
            if path in ("/health", "/ping", "/docs", "/openapi.json", "/postman.json"):
                continue

            tag = (operation.get("tags") or ["Other"])[0]
            if tag not in folders:
                tag = "Other"

            folders[tag].append(
                _request(
                    name=operation.get("summary") or f"{method.upper()} {path}",
                    method=method.upper(),
                    path=path,
                    description=_description(operation),
                    body=_body_for(path, operation),
                    query=_query_for(operation),
                    auth=True,
                )
            )

    items = [
        {"name": tag, "item": requests}
        for tag in _FOLDER_ORDER
        if (requests := folders.get(tag))
    ]

    return {
        "info": {
            "name": name,
            "_postman_id": "sendstack-public-api-v1",
            "description": (
                f"{spec['info']['title']} v{API_VERSION}.\n\n"
                "Set `base_url` and `api_token` in the environment, then run "
                "**Authentication -> Test authentication** first.\n\n"
                "Every request authenticates with `Authorization: Bearer {{api_token}}`."
            ),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        # Collection-level auth, inherited by every request. Set here rather
        # than repeated per request so rotating a key is one edit, and so a
        # request somebody adds by hand is authenticated by default.
        "auth": {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{api_token}}", "type": "string"}],
        },
        "variable": [
            {"key": "base_url", "value": base_url, "type": "string"},
        ],
        "item": items,
    }


def build_environment(*, base_url: str = "", api_token: str = "", name: str = "SendStack API") -> dict:
    """
    The Postman environment file.

    `api_token` defaults to empty on purpose -- see the module docstring.
    """
    values = []
    for key, default, _ in ENVIRONMENT_VARIABLES:
        value = {"base_url": base_url, "api_token": api_token}.get(key, default)
        values.append({
            "key": key,
            "value": value,
            "enabled": True,
            # Postman masks `secret` values in the UI and omits them from
            # shared workspaces. The token is the only one that needs it.
            "type": "secret" if key == "api_token" else "default",
        })

    return {
        "id": "sendstack-public-api-env",
        "name": name,
        "values": values,
        "_postman_variable_scope": "environment",
    }


# --- building one request ------------------------------------------------


def _request(*, name, method, path, description="", body=None, query=None, auth=True) -> dict:
    """One Postman request item."""
    # Postman wants the URL both as a string and as parsed parts. Giving it
    # only the string works until somebody edits a query parameter in the UI,
    # at which point Postman rewrites the whole URL from the parts it inferred.
    segments = [segment for segment in path.strip("/").split("/") if segment]
    url = {
        "raw": "{{base_url}}" + path,
        "host": ["{{base_url}}"],
        "path": segments,
    }
    if query:
        url["query"] = query
        # Only the enabled ones go in the raw string. Postman shows disabled
        # parameters struck through in the table either way, and putting them
        # in the URL makes a request that reads as `?status=&direction=&...`
        # before anybody has chosen a filter.
        enabled = [item for item in query if not item.get("disabled")]
        if enabled:
            url["raw"] = "{{base_url}}" + path + "?" + "&".join(
                f"{item['key']}={item['value']}" for item in enabled
            )

    headers = [{"key": "Accept", "value": "application/json"}]
    request = {
        "method": method,
        "header": headers,
        "url": url,
        "description": description,
    }

    if body is not None:
        headers.append({"key": "Content-Type", "value": "application/json"})
        request["body"] = {
            "mode": "raw",
            "raw": json.dumps(body, indent=2, ensure_ascii=False),
            "options": {"raw": {"language": "json"}},
        }

    if not auth:
        request["auth"] = {"type": "noauth"}

    return {"name": name, "request": request, "response": []}


def _description(operation) -> str:
    """The endpoint's prose, with its required scope spelled out underneath."""
    text = operation.get("description") or operation.get("summary") or ""
    security = operation.get("security")
    if security == []:
        return f"{text}\n\nNo authentication required.".strip()
    return text


def _body_for(path, operation):
    """
    The example request body, taken from the spec's own example.

    Returns None for an operation with no body, which is what tells `_request`
    not to send a Content-Type -- a GET carrying `Content-Type: application/json`
    and no body is the kind of detail that makes a generated collection look
    machine-made.
    """
    request_body = operation.get("requestBody") or {}
    content = (request_body.get("content") or {}).get("application/json") or {}
    schema = content.get("schema") or {}

    example = content.get("example")
    if example is None:
        # OpenAPI's *named* examples: `{"text": {"summary": ..., "value": {...}}}`.
        # The first one wins, which is why openapi.py lists the plainest case
        # first on every endpoint that has several -- the collection should
        # open on the request a developer can run without editing it.
        named = content.get("examples")
        if isinstance(named, dict) and named:
            first = next(iter(named.values()))
            example = first.get("value") if isinstance(first, dict) else first
    if example is None:
        examples = schema.get("examples")
        if isinstance(examples, list) and examples:
            example = examples[0]
    if example is None:
        # Nothing declared. Synthesised from the schema, which produces a body
        # with the right *shape* but not necessarily one that passes
        # validation -- so an endpoint whose body has interdependent fields
        # should carry an explicit example rather than rely on this.
        example = _example_from_schema(schema)
    if not example:
        return None

    if isinstance(example, dict):
        substitutions = _BODY_SUBSTITUTIONS.get(path)
        if substitutions:
            example = {**example, **{k: v for k, v in substitutions.items() if k in example}}
        omissions = _BODY_OMISSIONS.get(path)
        if omissions:
            example = {k: v for k, v in example.items() if k not in omissions}
    return example


def _example_from_schema(schema):
    """
    Synthesise a body from a schema's per-property examples.

    Only one level deep and only for objects. A full JSON-Schema example
    generator is a project of its own, and the spec carries explicit examples
    for the endpoints where the body is not obvious -- this is the fallback
    that keeps the simple ones from arriving empty.
    """
    if schema.get("type") != "object":
        return None
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    example = {}
    for name, definition in properties.items():
        examples = definition.get("examples")
        value = examples[0] if isinstance(examples, list) and examples else definition.get("default")
        if value is None and name not in required:
            continue
        if value is None:
            value = {"string": "", "integer": 0, "number": 0, "boolean": False,
                     "array": [], "object": {}}.get(definition.get("type"), "")
        example[name] = value
    return example or None


def _query_for(operation):
    """
    Query parameters, disabled by default.

    Postman sends only the enabled ones, so exporting the optional filters
    switched off gives a developer a menu of what is available without changing
    what the request does when they hit Send.
    """
    parameters = [p for p in operation.get("parameters") or [] if p.get("in") == "query"]
    if not parameters:
        return None
    return [
        {
            "key": parameter["name"],
            "value": str(_default_of(parameter)),
            "description": parameter.get("description", ""),
            "disabled": not parameter.get("required", False),
        }
        for parameter in parameters
    ]


def _default_of(parameter):
    schema = parameter.get("schema") or {}
    if schema.get("default") is not None:
        return schema["default"]
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        return examples[0]
    return ""
