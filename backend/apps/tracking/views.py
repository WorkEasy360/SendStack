import logging

from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponseRedirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.urls_safety import normalize_redirect_url

from . import services
from .models import TrackedLink
from .serializers import TrackedLinkSerializer

logger = logging.getLogger("sendstack")


@require_GET
@never_cache
def redirect_link(request, code):
    """
    The public end of a short link: record the click, then get out of the way.

    Unauthenticated by design -- the person clicking is the contact, not a
    user of the platform. Recording is best-effort so that a database hiccup
    costs a row of analytics rather than sending a customer to an error page.
    """
    link = TrackedLink.objects.filter(code=code).first()
    if link is None:
        raise Http404("Unknown link.")

    # Checked here, at the moment of the redirect, rather than trusted from the
    # row. The rewriter that writes these only ever matches http/https, so this
    # should never fire -- which is the point: it is what stands between a
    # future change to that pattern (or a row written before it existed) and
    # this endpoint sending a contact to a javascript:/data:/file: URL on our
    # own origin's say-so. A link that fails is a dead link, not a redirect.
    destination = normalize_redirect_url(link.target_url)
    if destination is None:
        logger.warning("Refused to redirect link %s: its destination is not an http(s) URL.", code)
        raise Http404("Unknown link.")

    try:
        forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
        services.record_click(
            link,
            ip_address=forwarded_for.split(",")[0].strip() or request.META.get("REMOTE_ADDR") or None,
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
            referer=request.META.get("HTTP_REFERER", ""),
        )
    except Exception:
        logger.exception("Failed to record click on link %s; redirecting anyway.", code)

    return HttpResponseRedirect(destination)


class TrackedLinkViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Read-only: links are created by sending, never by hand."""

    permission_classes = [IsAuthenticated]
    serializer_class = TrackedLinkSerializer
    filterset_fields = {"campaign": ["exact"], "contact": ["exact"], "click_count": ["gt"]}
    search_fields = ["original_url"]

    def get_queryset(self):
        return TrackedLink.objects.filter(tenant=self.request.user.tenant).select_related("contact", "campaign")

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """
        Per-URL click totals, optionally for one campaign.

        Grouped by the URL the tenant actually typed rather than by the
        per-recipient short code, because "how did this link do" is the
        question being asked -- 5,000 rows each with one click is not an
        answer anyone can read.
        """
        queryset = self.filter_queryset(self.get_queryset())
        rows = (
            queryset.values("original_url")
            .annotate(
                recipients=Count("id"),
                clicks=Sum("click_count"),
                unique_clicks=Count("id", filter=Q(click_count__gt=0)),
                contacts_clicked=Count("contact", filter=Q(click_count__gt=0), distinct=True),
            )
            .order_by("-clicks")
        )
        links = [
            {
                "url": row["original_url"],
                "recipients": row["recipients"],
                "clicks": row["clicks"] or 0,
                "unique_clicks": row["unique_clicks"],
                "contacts_clicked": row["contacts_clicked"],
                "click_rate": round(row["unique_clicks"] / row["recipients"] * 100, 1) if row["recipients"] else 0.0,
            }
            for row in rows
        ]
        return Response({
            "links": links,
            "total_clicks": sum(item["clicks"] for item in links),
            "total_recipients": sum(item["recipients"] for item in links),
        })

    @action(detail=False, methods=["get"])
    def clickers(self, request):
        """
        The contacts who clicked -- the retargeting audience.

        Returns ids so the caller can turn them straight into a campaign
        audience (`audience_type=CONTACT_IDS`) without a second round trip.
        """
        queryset = self.filter_queryset(self.get_queryset()).filter(click_count__gt=0, contact__isnull=False)
        contact_ids = list(queryset.values_list("contact_id", flat=True).distinct())
        return Response({"contact_ids": contact_ids, "count": len(contact_ids)})
