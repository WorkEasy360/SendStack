"""Public API views, grouped by resource."""

from .accounts import AccountDetailView, AccountListView
from .campaigns import (
    CampaignActionView,
    CampaignDetailView,
    CampaignListView,
    CampaignResultsView,
)
from .contacts import ContactDetailView, ContactListView
from .dashboard import ApiKeyViewSet, WebhookEndpointViewSet
from .health import DiagnosticsView, HealthView
from .media import MediaUploadView
from .messages import MessageDetailView, MessageListView, MessageSendView
from .meta import DocsView, OpenAPISchemaView, PingView, PostmanCollectionView
from .portal import (
    ApiOverviewView,
    ApiReferenceView,
    ApiScopeCatalogView,
    PostmanExportView,
    PostmanGuideView,
    TemplateApiExampleView,
)
from .reports import ReportDailyView, ReportSummaryView
from .templates import TemplateDetailView, TemplateListView
from .webhooks import (
    WebhookDeliveryListView,
    WebhookDetailView,
    WebhookListView,
    WebhookRotateSecretView,
    WebhookTestView,
)

__all__ = [
    "AccountDetailView",
    "AccountListView",
    "ApiKeyViewSet",
    "ApiOverviewView",
    "ApiReferenceView",
    "ApiScopeCatalogView",
    "CampaignActionView",
    "CampaignDetailView",
    "CampaignListView",
    "CampaignResultsView",
    "ContactDetailView",
    "ContactListView",
    "DiagnosticsView",
    "DocsView",
    "HealthView",
    "MediaUploadView",
    "MessageDetailView",
    "MessageListView",
    "MessageSendView",
    "OpenAPISchemaView",
    "PingView",
    "PostmanCollectionView",
    "PostmanExportView",
    "PostmanGuideView",
    "ReportDailyView",
    "ReportSummaryView",
    "TemplateApiExampleView",
    "TemplateDetailView",
    "TemplateListView",
    "WebhookDeliveryListView",
    "WebhookDetailView",
    "WebhookEndpointViewSet",
    "WebhookListView",
    "WebhookRotateSecretView",
    "WebhookTestView",
]
