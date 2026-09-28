"""
The developer portal's own endpoints -- what the API screens in the dashboard
read, as opposed to what an integration calls.

JWT-authenticated and mounted under ``/api/v1/developer/``, exactly like
`views/dashboard.py`, and separated from it only because these are *about* the
public API rather than administering it: none of them create, mutate or reveal
a credential.

Everything here derives from the same three sources the API itself uses --
`openapi.build_spec`, `scopes`, `template_api` -- rather than restating them.
That is the rule the brief is really asking for: the documentation page, the
Postman collection, the API tester and the backend cannot disagree if only one
of them knows anything.
"""

from django.conf import settings
from django.http import JsonResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.templates_app.models import MessageTemplate

from .. import scopes as scope_defs
from .. import template_api
from ..errors import ERROR_CODES
from ..events import EVENT_TYPES
from ..keys import LIVE_PREFIX, TEST_PREFIX
from ..openapi import API_VERSION, BASE_PATH, build_spec
from ..postman import ENVIRONMENT_VARIABLES, build_collection, build_environment


def _base_url(request) -> str:
    """
    The API base URL to *show the customer*, and it must be one that works.

    The deployment's declared public URL wins when it is set, because that is
    the address customers reach from outside and the one behind a proxy whose
    internal Host header would otherwise leak into the docs. When it is not set
    -- a development machine, or a deployment that never filled it in -- the
    request is the only source of truth available, and rebuilding from it is
    what makes this screen show `http://localhost/api/public/v1` rather than a
    placeholder nobody can paste anywhere.

    Never hardcoded, in either branch. See the brief's point 5: "from where do
    I take the URL?" is the question this function exists to answer.
    """
    return template_api.base_url(request)


class ApiOverviewView(APIView):
    """
    ``GET /api/v1/developer/api/overview`` -- everything the API home screen shows.

    One call rather than five, because this screen is the first thing a
    customer opens and four spinners resolving at four different moments is how
    a working page looks broken.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        base = _base_url(request)
        return Response({
            "base_url": base,
            # Both spelled out, because "which one do I use?" is the next
            # question after "where is the URL?" and a deployment that serves
            # both is the normal case during development.
            "environments": self._environments(request, base),
            "version": "v1",
            "spec_version": API_VERSION,
            "authentication": {
                "scheme": "bearer",
                "header": "Authorization",
                "format": "Bearer <your API token>",
                "example": f"Authorization: Bearer {LIVE_PREFIX}7f3a91c24b8e0d5a91c37e6f2b840195cd7ae3f1",
                "live_prefix": LIVE_PREFIX,
                "test_prefix": TEST_PREFIX,
                "notes": [
                    "The token goes in the Authorization header, never in the URL or a query string.",
                    "A token is shown once, when it is created. If you lose it, rotate the key.",
                    "Live tokens (ss_live_) send real messages. Test tokens (ss_test_) are refused by every send endpoint.",
                ],
            },
            "links": {
                "health": f"{base}/health",
                "diagnostics": f"{base}/diagnostics",
                "openapi": f"{base}/openapi.json",
                "reference": f"{base}/docs",
                "postman_collection": f"{base}/postman.json",
            },
            "rate_limits": {
                "requests_per_minute": settings.PUBLIC_API_DEFAULT_REQUESTS_PER_MINUTE,
                "messages_per_minute": settings.PUBLIC_API_DEFAULT_MESSAGES_PER_MINUTE,
                "campaigns_per_day": settings.PUBLIC_API_DEFAULT_CAMPAIGNS_PER_DAY,
                "note": (
                    "Workspace defaults. A token can be given tighter limits of its own when you "
                    "create it. Exceeding one returns 429 with a Retry-After header."
                ),
            },
            "quick_start": QUICK_START,
        })

    @staticmethod
    def _environments(request, base):
        """
        The base URL per environment, listed only when they genuinely differ.

        A single-environment deployment gets one row rather than a "Production"
        heading over the same URL twice, which reads like a configuration
        mistake.
        """
        declared = (getattr(settings, "PUBLIC_API_BASE_URL", "") or "").rstrip("/")
        from_request = f"{request.scheme}://{request.get_host()}{BASE_PATH}"
        rows = [{"name": "Production", "base_url": base, "current": True}]
        # Only when the deployment declares an address that differs from the
        # one this page was fetched over -- the api.acme.com / app.acme.com
        # split. Otherwise the two rows would be the same URL under two
        # headings, which reads like a configuration mistake.
        if declared and from_request != base:
            rows.append({
                "name": "This machine",
                "base_url": from_request,
                "current": False,
                "note": "The address you are reading this page from. Useful for local testing.",
            })
        return rows


#: The ten steps of the brief's quick start, in the product's own words and
#: pointing at the screens that actually exist. Data rather than markup so the
#: dashboard renders it, the docs page prints it and neither can drift.
QUICK_START = [
    {"step": 1, "title": "Create an API token",
     "detail": "Go to Developer -> API Tokens and click Create API token."},
    {"step": 2, "title": "Choose what it may do",
     "detail": "Pick the 'Send messages' preset. It grants exactly: send messages, view message status, "
               "view templates and view account status."},
    {"step": 3, "title": "Copy the token",
     "detail": "It is shown once, immediately after creation, and never again. Paste it somewhere safe now."},
    {"step": 4, "title": "Copy the API base URL",
     "detail": "It is on the API Overview tab. Everything below is relative to it."},
    {"step": 5, "title": "Import the Postman collection",
     "detail": "Download it from the Postman tab, along with the environment file, and import both into Postman."},
    {"step": 6, "title": "Fill in the environment",
     "detail": "Set base_url to the URL from step 4 and api_token to the token from step 3."},
    {"step": 7, "title": "Run GET {{base_url}}/health",
     "detail": "It is the first request in the collection, under Authentication."},
    {"step": 8, "title": "Check the answer",
     "detail": "authenticated: true means the token works. Anything else is explained by the error code in the body."},
    {"step": 9, "title": "Send a test message",
     "detail": "Use Messages -> Send a message. Set the phone variable to your own number first."},
    {"step": 10, "title": "Follow it",
     "detail": "The response carries a message_id. GET {{base_url}}/messages/<id> reports whether it was "
               "sent, delivered or read, and Developer -> API Logs shows the call you just made."},
]


class ApiScopeCatalogView(APIView):
    """
    ``GET /api/v1/developer/api/permissions`` -- the permission picker's contents.

    Separate from `ApiKeyViewSet.available_scopes` (which stays, unchanged, for
    anything already reading it) because this one carries the plain-English
    layer and the presets, and the older shape is part of what the existing
    create-key screen expects.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "permissions": scope_defs.SCOPE_DETAILS,
            "groups": list(dict.fromkeys(entry["group"] for entry in scope_defs.SCOPE_DETAILS)),
            "presets": scope_defs.SCOPE_PRESETS,
            "defaults": scope_defs.DEFAULT_SCOPES,
        })


class ApiReferenceView(APIView):
    """
    ``GET /api/v1/developer/api/reference`` -- the documentation page's content.

    A flattened, render-ready view of the OpenAPI document: one row per
    endpoint with its method, path, absolute URL, required scope, headers,
    example request and example responses. Flattening happens here rather than
    in the browser so the page has no OpenAPI knowledge and cannot mis-read the
    spec into a plausible-looking wrong example.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        spec = build_spec()
        base = _base_url(request)
        groups = {}

        for path, operations in spec["paths"].items():
            for method, operation in operations.items():
                if method not in ("get", "post", "put", "patch", "delete"):
                    continue
                tag = (operation.get("tags") or ["Other"])[0]
                groups.setdefault(tag, []).append(
                    self._endpoint(base, path, method, operation)
                )

        # Spec tag order, not dict order: it is the order somebody should read
        # them in, and it starts at Authentication for a reason.
        ordered = [
            {"name": tag["name"], "description": tag["description"], "endpoints": groups.pop(tag["name"], [])}
            for tag in spec["tags"]
        ]
        ordered.extend(
            {"name": name, "description": "", "endpoints": endpoints}
            for name, endpoints in groups.items()
        )

        return Response({
            "base_url": base,
            "groups": [group for group in ordered if group["endpoints"]],
            "error_codes": [
                {"code": code, "description": description}
                for code, description in sorted(ERROR_CODES.items())
            ],
            "webhook_events": [
                {"event": name, "description": description} for name, description in EVENT_TYPES.items()
            ],
            "permissions": scope_defs.SCOPE_DETAILS,
        })

    def _endpoint(self, base, path, method, operation) -> dict:
        body = self._example_body(path, operation)
        scope = self._scope_of(operation)
        return {
            "id": f"{method}-{path}".replace("/", "-").replace("{", "").replace("}", ""),
            "method": method.upper(),
            "path": path,
            "url": f"{base}{path}",
            "summary": operation.get("summary", ""),
            "description": operation.get("description", ""),
            "authentication": self._authentication_of(operation),
            "required_permission": scope,
            "required_permission_label": scope_defs.label_for(scope) if scope else None,
            "headers": self._headers(operation, body is not None),
            "parameters": [
                {
                    "name": parameter["name"],
                    "in": parameter.get("in", "query"),
                    "required": parameter.get("required", False),
                    "description": parameter.get("description", ""),
                }
                for parameter in operation.get("parameters") or []
            ],
            "request_body": body,
            "responses": self._responses(operation),
            "curl": self._curl(base, path, method, body),
        }

    @staticmethod
    def _authentication_of(operation) -> str:
        """
        Whether this endpoint needs a token, and OpenAPI's three ways of saying so.

        `security: []` is "none accepted". A list *containing* the empty
        requirement `{}` is "optional" -- which is `/health`, and the
        distinction matters: telling somebody it needs a token would stop them
        using it as the uptime check it is meant to be.
        """
        security = operation.get("security")
        if security == []:
            return "None"
        if isinstance(security, list) and any(not entry for entry in security):
            return "Optional bearer token"
        return "Bearer token"

    @staticmethod
    def _scope_of(operation):
        """
        The scope an endpoint needs, read out of its own description.

        The descriptions in openapi.py already name it (`Requires the
        \\`messages:send\\` scope.`) and that sentence is what a developer reads,
        so parsing it keeps one statement of the requirement rather than two
        that can disagree. Anything not found returns None and the page says
        nothing rather than guessing.
        """
        text = operation.get("description") or ""
        for scope in scope_defs.ALL_SCOPES:
            if f"`{scope}`" in text:
                return scope
        return None

    @staticmethod
    def _headers(operation, has_body) -> dict:
        headers = {}
        if operation.get("security") != []:
            headers["Authorization"] = f"Bearer {LIVE_PREFIX}YOUR_API_TOKEN"
        if has_body:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _example_body(path, operation):
        # The very function the Postman collection builds its bodies with, so
        # the JSON on the docs page and the JSON in the imported request are
        # the same bytes rather than two renderings of the same idea.
        from ..postman import _body_for

        return _body_for(path, operation)

    @staticmethod
    def _responses(operation) -> list:
        """
        Every documented outcome, success and failure alike.

        Failures are included because the brief asks for them and because an
        endpoint documented only by its happy path is an endpoint whose error
        handling gets written after the first outage.
        """
        rows = []
        for status_code, response in (operation.get("responses") or {}).items():
            content = (response.get("content") or {}).get("application/json") or {}
            rows.append({
                "status": status_code,
                "description": response.get("description", ""),
                "example": content.get("example"),
                "is_error": not str(status_code).startswith("2"),
            })
        rows.sort(key=lambda row: str(row["status"]))
        return rows

    @staticmethod
    def _curl(base, path, method, body) -> str:
        import json

        lines = [f"curl -X {method.upper()} {base}{path} \\"]
        lines.append(f"  -H 'Authorization: Bearer {LIVE_PREFIX}YOUR_API_TOKEN'")
        if body is not None:
            lines[-1] += " \\"
            lines.append("  -H 'Content-Type: application/json' \\")
            payload = json.dumps(body, indent=2, ensure_ascii=False).replace("\n", "\n  ")
            lines.append(f"  -d '{payload}'")
        return "\n".join(lines)


class TemplateApiExampleView(APIView):
    """
    ``GET /api/v1/developer/api/templates`` and ``/api/v1/developer/api/templates/<pk>``.

    The generated API JSON for the workspace's templates -- what the brief's
    point 7 asks for, and what the Templates screen's "API JSON" panel renders.
    Tenant-scoped through the queryset, so a template id belonging to another
    workspace is a 404 rather than somebody else's message body.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk=None):
        queryset = MessageTemplate.objects.filter(tenant=request.user.tenant)

        if pk is not None:
            template = queryset.filter(pk=pk).first()
            if template is None:
                return Response({"detail": "No such template."}, status=404)
            return Response(template_api.describe(template, request=request))

        return Response({
            "base_url": _base_url(request),
            "results": [
                template_api.describe(template, request=request)
                for template in queryset.order_by("name")
            ],
        })


class PostmanExportView(APIView):
    """
    ``GET /api/v1/developer/api/postman?type=collection|environment``.

    The authenticated download. The collection is identical to the public one
    at ``/postman.json``; the environment is the reason this endpoint exists at
    all, because it can be pre-filled with this deployment's real base URL --
    the single value a customer is most likely to get wrong.

    The token is **not** pre-filled and cannot be: it is not recoverable from
    the database, which is the whole design. The file ships with an empty
    `api_token` and the UI says which box to paste into.
    """

    permission_classes = [IsAuthenticated]

    FILENAMES = {
        "collection": "WhatsApp Marketing API.postman_collection.json",
        "environment": "WhatsApp Marketing API.postman_environment.json",
    }

    def get(self, request):
        base = _base_url(request)
        wanted = (request.query_params.get("type") or "collection").strip().lower()
        if wanted not in self.FILENAMES:
            return Response({"detail": "type must be 'collection' or 'environment'."}, status=400)

        document = (
            build_collection(base_url=base)
            if wanted == "collection"
            else build_environment(base_url=base)
        )
        response = JsonResponse(document, json_dumps_params={"indent": 2})
        if request.query_params.get("download") in ("1", "true", "yes"):
            response["Content-Disposition"] = f'attachment; filename="{self.FILENAMES[wanted]}"'
        return response


class PostmanGuideView(APIView):
    """
    ``GET /api/v1/developer/api/postman/guide`` -- what the Postman tab explains.

    The variable list comes from `postman.ENVIRONMENT_VARIABLES`, so the page
    documents the file it is offering rather than a remembered version of it.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        base = _base_url(request)
        return Response({
            "base_url": base,
            "collection_url": f"{base}/postman.json",
            "variables": [
                {
                    "key": key,
                    "example": base if key == "base_url" else default,
                    "description": description,
                    "secret": key == "api_token",
                }
                for key, default, description in ENVIRONMENT_VARIABLES
            ],
            "steps": [
                "Download both files below, or import the collection straight from its URL.",
                "In Postman, click Import and drop both files in.",
                "Select the 'WhatsApp Marketing API' environment in the top-right dropdown.",
                "Open the environment and paste your API token into api_token. base_url is already filled in.",
                "Open Authentication -> Test authentication and hit Send.",
                "A 200 with authenticated: true means everything is wired up correctly.",
            ],
        })
