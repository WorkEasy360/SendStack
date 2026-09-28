"""
The public API's URL map.

Mounted at ``/api/public/v1/`` rather than ``/api/v1/``, because ``/api/v1/`` is
already the SPA's own API and five of the names this API needs -- campaigns,
templates, contacts, media, reports -- are taken there. Sharing the prefix would
mean either shadowing endpoints the frontend depends on or giving the public
resources second-class names.

The separation is worth more than the shorter path anyway: it makes "is this
request from a customer's browser or from somebody's integration?" answerable
from the path alone, in nginx, in a log, and in a rate-limit rule. A deployment
that wants the cosmetic URL can map ``api.<domain>/v1/*`` onto this prefix at
the proxy without changing a line of Python.

Ordering matters in two places, both marked below: a literal path that could be
captured by a following pattern with a variable in the same position has to come
first.
"""

from django.urls import path

from .views import (
    AccountDetailView,
    AccountListView,
    CampaignActionView,
    CampaignDetailView,
    CampaignListView,
    CampaignResultsView,
    ContactDetailView,
    ContactListView,
    DiagnosticsView,
    DocsView,
    HealthView,
    MediaUploadView,
    MessageDetailView,
    MessageListView,
    MessageSendView,
    OpenAPISchemaView,
    PingView,
    PostmanCollectionView,
    ReportDailyView,
    ReportSummaryView,
    TemplateDetailView,
    TemplateListView,
    WebhookDeliveryListView,
    WebhookDetailView,
    WebhookListView,
    WebhookRotateSecretView,
    WebhookTestView,
)

app_name = "publicapi"

urlpatterns = [
    # Documentation and health. Public; see views/meta.py.
    path("docs", DocsView.as_view(), name="docs"),
    path("docs/", DocsView.as_view()),
    path("openapi.json", OpenAPISchemaView.as_view(), name="openapi"),
    path("postman.json", PostmanCollectionView.as_view(), name="postman"),

    # "Is this working?", asked the two ways somebody means it. See views/health.py.
    # `/health` is the documented name and the one the Postman collection and
    # the quick start use; `/ping` predates it and stays as an alias forever,
    # because an integration built against a published path must not be broken
    # by a later endpoint having a better name.
    path("health", HealthView.as_view(), name="health"),
    path("ping", PingView.as_view(), name="ping"),
    path("diagnostics", DiagnosticsView.as_view(), name="diagnostics"),

    # Messages. `/messages/send` must precede `/messages/<message_id>`, or
    # "send" is read as a message id and every send returns 404.
    path("messages/send", MessageSendView.as_view(), name="message-send"),
    path("messages", MessageListView.as_view(), name="message-list"),
    path("messages/<str:message_id>", MessageDetailView.as_view(), name="message-detail"),

    # Campaigns.
    path("campaigns", CampaignListView.as_view(), name="campaign-list"),
    path("campaigns/<str:campaign_id>", CampaignDetailView.as_view(), name="campaign-detail"),
    path("campaigns/<str:campaign_id>/results", CampaignResultsView.as_view(), name="campaign-results"),
    path(
        "campaigns/<str:campaign_id>/<str:action>",
        CampaignActionView.as_view(),
        name="campaign-action",
    ),

    # Templates.
    path("templates", TemplateListView.as_view(), name="template-list"),
    path("templates/<str:template_id>", TemplateDetailView.as_view(), name="template-detail"),

    # Contacts.
    path("contacts", ContactListView.as_view(), name="contact-list"),
    path("contacts/<str:contact_id>", ContactDetailView.as_view(), name="contact-detail"),

    # Accounts.
    path("accounts", AccountListView.as_view(), name="account-list"),
    path("accounts/<str:account_id>", AccountDetailView.as_view(), name="account-detail"),

    # Media.
    path("media", MediaUploadView.as_view(), name="media-upload"),

    # Reports.
    path("reports/summary", ReportSummaryView.as_view(), name="report-summary"),
    path("reports/daily", ReportDailyView.as_view(), name="report-daily"),

    # Webhooks. `/webhooks/deliveries` must precede `/webhooks/<endpoint_id>`
    # for the same reason `/messages/send` does.
    path("webhooks", WebhookListView.as_view(), name="webhook-list"),
    path("webhooks/deliveries", WebhookDeliveryListView.as_view(), name="webhook-deliveries"),
    path("webhooks/<int:endpoint_id>", WebhookDetailView.as_view(), name="webhook-detail"),
    path("webhooks/<int:endpoint_id>/test", WebhookTestView.as_view(), name="webhook-test"),
    path(
        "webhooks/<int:endpoint_id>/rotate-secret",
        WebhookRotateSecretView.as_view(),
        name="webhook-rotate-secret",
    ),
]
