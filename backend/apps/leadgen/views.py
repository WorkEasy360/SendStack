import json
import logging

from django.db.models import Count
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.urls_safety import normalize_redirect_url

from . import services
from .models import ClickToChatLink, LeadEvent
from .serializers import ClickToChatLinkSerializer, LeadEventSerializer

logger = logging.getLogger("sendstack")


@require_GET
@never_cache
def open_chat(request, code):
    """
    The public end of a click-to-chat link: count the tap, then hand over to
    WhatsApp.

    A link whose number is not connected yet sends nobody anywhere -- a wa.me
    URL built from a blank number opens WhatsApp's own error page, which looks
    to the customer as though the business does not exist.
    """
    link = ClickToChatLink.objects.filter(code=code, is_active=True).select_related("whatsapp_account").first()
    if link is None:
        raise Http404("Unknown link.")

    # whatsapp_url builds a wa.me link out of the account's own number, so this
    # should always pass. It is checked anyway because this endpoint is a public
    # redirect: the one class of bug that turns it into an open redirect is a
    # destination that stopped being what the builder intended.
    destination = normalize_redirect_url(services.whatsapp_url(link))
    if not destination:
        return HttpResponse(
            "This chat link is not ready yet. Please try again shortly.", status=503, content_type="text/plain",
        )

    try:
        forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
        services.record_visit(
            link,
            ip_address=forwarded_for.split(",")[0].strip() or request.META.get("REMOTE_ADDR") or None,
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
            referer=request.META.get("HTTP_REFERER", ""),
        )
    except Exception:
        logger.exception("Failed to record a visit to chat link %s; redirecting anyway.", code)

    return HttpResponseRedirect(destination)


@require_GET
def widget_script(request, code):
    """
    The embeddable button, as a script the customer's site includes.

    Served from here rather than shipped as a file the tenant copies so that
    a change to the button reaches every site that embedded it, and so the
    snippet they paste never contains anything they have to keep in step.
    """
    link = ClickToChatLink.objects.filter(code=code, is_active=True).select_related("whatsapp_account").first()
    if link is None:
        raise Http404("Unknown link.")

    config = {
        "url": services.short_url(link),
        "label": link.name,
        "greeting": (link.prefilled_message or "Chat with us on WhatsApp")[:160],
    }
    script = _WIDGET_TEMPLATE.replace("__CONFIG__", json.dumps(config))
    response = HttpResponse(script, content_type="application/javascript")
    # Short, not long: a tenant who renames the button expects the site to
    # catch up today, and this is a few hundred bytes.
    response["Cache-Control"] = "public, max-age=300"
    return response


_WIDGET_TEMPLATE = """/* SendStack click-to-chat widget */
(function () {
  var config = __CONFIG__;
  if (document.getElementById("sendstack-chat-widget")) return;

  var style = document.createElement("style");
  style.textContent =
    "#sendstack-chat-widget{position:fixed;right:20px;bottom:20px;z-index:2147483000;" +
    "display:flex;align-items:center;gap:10px;padding:12px 18px;border-radius:999px;" +
    "background:#25D366;color:#fff;font:600 15px/1 system-ui,-apple-system,Segoe UI,sans-serif;" +
    "text-decoration:none;box-shadow:0 8px 24px rgba(0,0,0,.18);transition:transform .15s ease}" +
    "#sendstack-chat-widget:hover{transform:translateY(-2px)}" +
    "#sendstack-chat-widget svg{width:22px;height:22px;fill:currentColor}" +
    "@media (max-width:480px){#sendstack-chat-widget span{display:none}" +
    "#sendstack-chat-widget{padding:14px;border-radius:50%}}";
  document.head.appendChild(style);

  var anchor = document.createElement("a");
  anchor.id = "sendstack-chat-widget";
  anchor.href = config.url;
  anchor.target = "_blank";
  anchor.rel = "noopener";
  anchor.setAttribute("aria-label", config.greeting);
  anchor.innerHTML =
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12.04 2C6.58 2 2.13 6.45 2.13 11.91c0 2.1.55 4.06 1.6 5.8L2 22l4.42-1.68a9.86 9.86 0 0 0 5.62 1.74h.01c5.46 0 9.91-4.45 9.91-9.91 0-2.65-1.03-5.14-2.9-7.01A9.82 9.82 0 0 0 12.04 2Zm0 18.15h-.01a8.2 8.2 0 0 1-4.94-1.6l-.35-.24-2.62 1 .7-2.56-.23-.36a8.19 8.19 0 0 1-1.26-4.38c0-4.54 3.7-8.24 8.25-8.24 2.2 0 4.27.86 5.83 2.42a8.19 8.19 0 0 1 2.41 5.83c0 4.54-3.7 8.13-8.24 8.13Z"/></svg>' +
    "<span></span>";
  anchor.querySelector("span").textContent = config.greeting;

  function mount() { document.body.appendChild(anchor); }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
"""


class ClickToChatLinkViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ClickToChatLinkSerializer
    filterset_fields = {"is_active": ["exact"], "whatsapp_account": ["exact"]}
    search_fields = ["name"]

    def get_queryset(self):
        return ClickToChatLink.objects.filter(tenant=self.request.user.tenant).select_related("whatsapp_account")

    def perform_create(self, serializer):
        serializer.save(
            tenant=self.request.user.tenant, created_by=self.request.user, code=services.generate_code(),
        )

    @action(detail=True, methods=["get"], url_path="qr")
    def qr(self, request, pk=None):
        """The QR as SVG, ready to drop into a poster or print at any size."""
        link = self.get_object()
        return HttpResponse(services.qr_svg(link), content_type="image/svg+xml")

    @action(detail=True, methods=["get"])
    def snippet(self, request, pk=None):
        """Everything the tenant needs to put this link somewhere."""
        link = self.get_object()
        base = services.public_base_url()
        return Response({
            "short_url": services.short_url(link),
            "whatsapp_url": services.whatsapp_url(link),
            "qr_url": f"{base}/api/v1/leadgen/links/{link.pk}/qr/",
            "embed_snippet": f'<script src="{base}/w/{link.code}.js" async></script>',
        })


class LeadEventViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = LeadEventSerializer
    filterset_fields = {"kind": ["exact"], "link": ["exact"], "contact": ["exact"]}

    def get_queryset(self):
        return LeadEvent.objects.filter(tenant=self.request.user.tenant).select_related("contact", "link")

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """
        Leads by source.

        Counted by distinct contact, not by event: the question is how many
        people a channel brought, and one person tapping the poster twice is
        still one person.
        """
        rows = (
            self.get_queryset()
            .values("kind")
            .annotate(contacts=Count("contact", distinct=True), events=Count("id"))
            .order_by("-contacts")
        )
        by_link = (
            self.get_queryset().filter(link__isnull=False)
            .values("link", "link__name")
            .annotate(contacts=Count("contact", distinct=True))
            .order_by("-contacts")
        )
        return Response({
            "by_kind": list(rows),
            "by_link": [
                {"link": row["link"], "name": row["link__name"], "contacts": row["contacts"]} for row in by_link
            ],
        })
