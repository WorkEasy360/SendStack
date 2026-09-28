"""
The endpoints that describe the API rather than doing anything with it.

`/docs` and `/openapi.json` are **public and unauthenticated**, deliberately.
Documentation somebody has to authenticate to read is documentation they cannot
read while deciding whether to integrate, and neither of these exposes a single
piece of tenant data -- the schema is a static description of the interface.

`/ping` is authenticated but scope-free. It is what an integration calls during
setup to answer "does this key work, and what did the customer actually tick?",
which has to be answerable *before* it knows whether it has any scopes at all.
"""

from django.http import JsonResponse
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_control
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from ..errors import ERROR_CODES
from ..events import ALL_EVENT_TYPES, EVENT_TYPES
from ..openapi import build_spec, server_url
from ..scopes import SCOPES
from .base import PublicAPIView


class PingView(PublicAPIView):
    """``GET /ping`` -- confirm a key works and report what it may do."""

    # An empty list, not None. `None` means "nobody decided", which
    # HasApiScope refuses; `[]` means "decided: none needed". The distinction
    # is the whole reason permissions.py treats them differently.
    required_scopes = []

    def get(self, request):
        api_key = request.auth
        return self.ok({
            "workspace": api_key.tenant.name,
            "workspace_id": api_key.tenant_id,
            "key_name": api_key.name,
            "key_prefix": api_key.masked_key,
            "environment": api_key.environment,
            "scopes": api_key.scopes or [],
            "rate_limits": {
                "requests_per_minute": api_key.requests_per_minute or None,
                "messages_per_minute": api_key.messages_per_minute or None,
                "campaigns_per_day": api_key.campaigns_per_day or None,
            },
            "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
        })


class OpenAPISchemaView(APIView):
    """``GET /openapi.json``."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    # `method_decorator`, because cache_control expects a function view and
    # would otherwise be handed `self` as the request.
    @method_decorator(cache_control(public=True, max_age=300))
    def get(self, request):
        # A plain JsonResponse rather than DRF's Response: the schema is a
        # static document, not an API resource, and it must not be wrapped in
        # this API's success envelope -- every OpenAPI tool would reject it.
        return JsonResponse(build_spec(request), json_dumps_params={"indent": 2})


class DocsView(APIView):
    """``GET /docs`` -- the human-readable reference."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    def get(self, request):
        # `QUICK_START` is imported from the portal views rather than restated
        # here: the dashboard renders the same ten steps, and two copies of a
        # walkthrough is two walkthroughs that stop agreeing the first time a
        # screen is renamed.
        from .portal import QUICK_START

        base = server_url(request)
        return render(request, "publicapi/docs.html", {
            "base_url": base,
            "scopes": SCOPES.items(),
            "error_codes": sorted(ERROR_CODES.items()),
            "events": [(name, EVENT_TYPES[name]) for name in ALL_EVENT_TYPES],
            "schema_url": f"{base}/openapi.json",
            "postman_url": f"{base}/postman.json",
            "quick_start": QUICK_START,
        })


class PostmanCollectionView(APIView):
    """
    ``GET /postman.json`` -- the collection, importable straight from this URL.

    Public and unauthenticated for the same reason `/openapi.json` is: it is a
    description of the interface, it carries no tenant data, and requiring a
    credential to fetch it would break Postman's "Import -> Link" flow, which is
    the shortest path a customer has from reading the docs to making a call.

    No token is baked in -- see postman.build_environment. The environment file
    the dashboard offers can carry one, because that download is authenticated
    and initiated by the person who owns the key.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    @method_decorator(cache_control(public=True, max_age=300))
    def get(self, request):
        from ..postman import build_collection

        collection = build_collection(base_url=server_url(request))
        response = JsonResponse(collection, json_dumps_params={"indent": 2})
        # So a browser hitting the URL saves a file with the name the brief
        # names, rather than rendering 60KB of JSON in a tab.
        if request.query_params.get("download") in ("1", "true", "yes"):
            response["Content-Disposition"] = (
                'attachment; filename="WhatsApp Marketing API.postman_collection.json"'
            )
        return response
