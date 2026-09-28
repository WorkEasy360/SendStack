"""
The developer settings screens' endpoints.

Mounted under the SPA's own ``/api/v1/developer/`` prefix and authenticated
with the ordinary JWT, because these are dashboard screens and not part of the
public contract. Kept in a separate module from `urls.py` so the two mount
points can never be confused for each other -- everything in that file is
reachable with an API key and nothing in this one is.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ApiKeyViewSet,
    ApiOverviewView,
    ApiReferenceView,
    ApiScopeCatalogView,
    PostmanExportView,
    PostmanGuideView,
    TemplateApiExampleView,
    WebhookEndpointViewSet,
)

router = DefaultRouter()
router.register("api-keys", ApiKeyViewSet, basename="developer-api-key")
router.register("webhooks", WebhookEndpointViewSet, basename="developer-webhook")

urlpatterns = [
    # The developer-portal screens. Under `api/` so they cannot ever collide
    # with a router basename -- `api-keys/scopes/` and `api/permissions` are
    # two different things and the prefix is what keeps them that way.
    path("api/overview", ApiOverviewView.as_view(), name="developer-api-overview"),
    path("api/permissions", ApiScopeCatalogView.as_view(), name="developer-api-permissions"),
    path("api/reference", ApiReferenceView.as_view(), name="developer-api-reference"),
    path("api/postman", PostmanExportView.as_view(), name="developer-api-postman"),
    path("api/postman/guide", PostmanGuideView.as_view(), name="developer-api-postman-guide"),
    # The literal `templates` list must precede the `<int:pk>` form for the
    # same reason `/messages/send` precedes `/messages/<id>` in urls.py.
    path("api/templates", TemplateApiExampleView.as_view(), name="developer-api-templates"),
    path("api/templates/<int:pk>", TemplateApiExampleView.as_view(), name="developer-api-template"),

    path("", include(router.urls)),
]
