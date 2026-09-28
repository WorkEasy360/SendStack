from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from apps.audit.services import log_event

from . import services
from .documents_registry import ACCEPTANCE_REQUIRED, DOCUMENTS, render, summary
from .serializers import LegalAcceptanceSerializer


class LegalDocumentListView(APIView):
    """
    Every legal document and its current version. Public.

    Unauthenticated on purpose: somebody deciding whether to sign up has to be
    able to read the terms before they have an account, and a privacy policy
    behind a login is not a privacy policy.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle]

    def get(self, request):
        return Response({"results": summary()})


class LegalDocumentView(APIView):
    """One document's full text. Public, for the same reason."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle]

    def get(self, request, slug):
        if slug not in DOCUMENTS:
            return Response({"detail": "No such document."}, status=status.HTTP_404_NOT_FOUND)
        return Response(render(slug))


class LegalPendingView(APIView):
    """What this user still has to accept, and what they have accepted already."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "pending": services.outstanding_for(request.user),
            "accepted": LegalAcceptanceSerializer(services.history_for(request.user), many=True).data,
        })


class LegalAcceptView(APIView):
    """
    Record that this user accepts a document, at whatever version is published now.

    The body names only the document. The version comes from the server, so a
    caller cannot record acceptance of wording it was never shown -- see
    services.record_acceptance.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        slugs = request.data.get("documents")
        if isinstance(slugs, str):
            slugs = [slugs]
        if not slugs:
            single = request.data.get("document")
            slugs = [single] if single else list(ACCEPTANCE_REQUIRED)

        unknown = [slug for slug in slugs if slug not in DOCUMENTS]
        if unknown:
            return Response(
                {"detail": f"Unknown document: {', '.join(unknown)}."}, status=status.HTTP_400_BAD_REQUEST
            )

        recorded = [services.record_acceptance(request.user, slug, request=request) for slug in slugs]
        log_event(
            request=request, action="legal.accepted", tenant=request.user.tenant,
            object_type="legal.LegalAcceptance",
            metadata={"documents": [{"slug": row.document, "version": row.version} for row in recorded]},
        )
        return Response(
            {
                "accepted": LegalAcceptanceSerializer(recorded, many=True).data,
                "pending": services.outstanding_for(request.user),
            },
            status=status.HTTP_201_CREATED,
        )
