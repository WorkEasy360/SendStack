"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts.views import LoginView
from apps.leadgen.views import open_chat, widget_script
from apps.tracking.views import redirect_link
from apps.voice import webhooks as voice_webhooks


def home(request):
    return JsonResponse({
        "application": "SendStack",
        "status": "running",
        "version": "1.0",
    })


urlpatterns = [
    path("", home, name="home"),

    # Mount point is configurable (DJANGO_ADMIN_URL) so a deployment can move
    # it off the path every scanner tries first. core.middleware.
    # AdminSecurityMiddleware reads the same setting to know what to protect.
    path(settings.DJANGO_ADMIN_URL, admin.site.urls),

    path("health/", include("apps.health.urls")),

    # Public, unauthenticated, and deliberately short: this is the URL a
    # contact actually taps in WhatsApp, so it lives at the root rather
    # than under /api/.
    path("r/<str:code>", redirect_link, name="tracked-link-redirect"),

    # Click-to-chat: the link printed on a poster or put in a bio, and
    # the script a customer embeds on their own website. Both are public
    # and both are short, because both are typed or pasted by hand.
    path("c/<str:code>", open_chat, name="click-to-chat"),
    path("w/<str:code>.js", widget_script, name="click-to-chat-widget"),

    # The phone carrier's callbacks during a live call. Public and
    # unauthenticated by construction -- Twilio has no credential of ours to
    # present -- so each URL carries an unguessable per-call token and every
    # request is checked against the carrier's own signature. They sit at the
    # root rather than under /api/ because the carrier is handed these URLs
    # literally and signs them exactly as given.
    path("voice/answer/<str:token>/", voice_webhooks.answer, name="voice-answer"),
    # Addressed by a per-tenant token: an incoming call has no session yet,
    # so this URL is the only thing that says which tenant is being rung.
    path("voice/incoming/<str:token>/", voice_webhooks.incoming, name="voice-incoming"),
    path("voice/turn/<str:token>/", voice_webhooks.turn, name="voice-turn"),
    path("voice/status/<str:token>/", voice_webhooks.status, name="voice-status"),

    path(
        "api/schema/",
        SpectacularAPIView.as_view(),
        name="schema",
    ),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="api_docs",
    ),

    path(
        "api/auth/token/",
        LoginView.as_view(),
        name="token_obtain_pair",
    ),

    path(
        "api/auth/token/refresh/",
        TokenRefreshView.as_view(),
        name="token_refresh",
    ),

    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/tenants/", include("apps.tenants.urls")),
    path("api/v1/billing/", include("apps.subscriptions.urls")),
    # Payments sit under the same customer-facing prefix as the plans
    # and usage they act on, with their own disjoint sub-paths.
    path("api/v1/billing/", include("apps.billing.urls")),
    path("api/v1/onboarding/", include("apps.onboarding.urls")),
    path("api/v1/whatsapp/", include("apps.whatsapp.urls")),
    path("api/v1/contacts/", include("apps.contacts.urls")),
    path("api/v1/templates/", include("apps.templates_app.urls")),
    path("api/v1/campaigns/", include("apps.campaigns.urls")),
    path("api/v1/messaging/", include("apps.messaging.urls")),
    path("api/v1/media/", include("apps.media_library.urls")),
    path("api/v1/credits/", include("apps.credits.urls")),
    path("api/v1/inbox/", include("apps.inbox.urls")),
    path("api/v1/automation/", include("apps.automation.urls")),
    path("api/v1/crm/", include("apps.crm.urls")),
    path("api/v1/compliance/", include("apps.compliance.urls")),
    path("api/v1/tracking/", include("apps.tracking.urls")),
    path("api/v1/teams/", include("apps.teams.urls")),
    path("api/v1/ai/", include("apps.ai.urls")),
    # The chatbot dashboard. No webhook of its own: inbound WhatsApp traffic
    # already arrives at the per-account endpoint under /api/v1/whatsapp/,
    # which is signature-verified and deduplicated, and a second inbound path
    # would be a second thing to secure for no benefit.
    path("api/v1/chatbot/", include("apps.chatbot.urls")),
    path("api/v1/voice/", include("apps.voice.urls")),
    path("api/v1/realtime/", include("apps.realtime.urls")),
    path("api/v1/leadgen/", include("apps.leadgen.urls")),
    # The number filter and the group grabber: lookups through a linked
    # number that produce contacts, never consent.
    path("api/v1/leadtools/", include("apps.leadtools.urls")),
    path("api/v1/notifications/", include("apps.notifications.urls")),
    path("api/v1/reports/", include("apps.reports.urls")),
    path("api/v1/support/", include("apps.support.urls")),
    # Public: the documents have to be readable before somebody has an
    # account, and the acceptance endpoints below them are authenticated.
    path("api/v1/legal/", include("apps.legal.urls")),

    # Settings -> Developer. Ordinary JWT-authenticated dashboard screens for
    # managing API keys and webhook endpoints -- deliberately *not* reachable
    # with an API key, because a key that could mint another key would make
    # revoking one pointless.
    path("api/v1/developer/", include("apps.publicapi.dashboard_urls")),

    # **The public developer API.** Its own prefix rather than /api/v1/,
    # which is already the SPA's and where campaigns, templates, contacts,
    # media and reports are all taken. See apps/publicapi/urls.py.
    #
    # Authenticated only by API key, with its own error envelope, its own rate
    # limits and its own OpenAPI document at /api/public/v1/openapi.json.
    path("api/public/v1/", include("apps.publicapi.urls")),
]

# Uploaded attachments are served by nginx in every deployed environment (see
# nginx/default.conf). Under `runserver` there is no nginx, so without this an
# uploaded image 404s in the picker and cannot be fetched by a provider.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
