from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, viewsets
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import log_event

from .keywords import is_start, is_stop
from .models import CompliancePolicy, ConsentEvent
from .serializers import CompliancePolicySerializer, ConsentEventSerializer


class CompliancePolicyView(RetrieveUpdateAPIView):
    """
    The tenant's single compliance policy.

    A singleton rather than a collection: there is one set of rules per
    tenant, and the row is created on first read so the settings page never
    has to deal with "not created yet" as a separate state.
    """

    serializer_class = CompliancePolicySerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        policy, _ = CompliancePolicy.objects.get_or_create(tenant=self.request.user.tenant)
        return policy

    def perform_update(self, serializer):
        policy = serializer.save()
        log_event(
            request=self.request, action="compliance.policy_updated", tenant=policy.tenant,
            object_type="compliance_policy", object_id=policy.pk,
            metadata={"quiet_hours": policy.quiet_hours_enabled, "stop_keywords": policy.stop_keywords_enabled},
        )


class KeywordPreviewView(APIView):
    """
    Answers "would this message unsubscribe someone?" for a phrase typed into
    the settings page.

    Worth being an endpoint rather than a client-side copy of the matcher: the
    whole point is to show what the *server* will do with a message, and a
    second implementation in JavaScript would eventually disagree with it.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        policy = CompliancePolicy.for_tenant(request.user.tenant)
        text = request.data.get("text", "")
        return Response({
            "text": text,
            "stops": is_stop(text, policy.extra_stop_keywords),
            "starts": is_start(text, policy.extra_start_keywords),
        })


class ConsentEventViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Read-only: consent history is evidence, so nothing edits or deletes it."""

    serializer_class = ConsentEventSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["contact", "event", "source"]

    def get_queryset(self):
        return ConsentEvent.objects.filter(tenant=self.request.user.tenant).select_related("contact", "actor")
