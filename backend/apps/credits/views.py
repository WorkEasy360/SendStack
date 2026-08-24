from django.contrib.auth import get_user_model
from rest_framework import mixins, status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import log_event

from .models import CreditTransaction
from .serializers import AssignCreditsSerializer, CreditTransactionSerializer, DeductCreditsSerializer
from .services import CreditError, InsufficientCreditsError, assign_credits, deduct_credits, visible_accounts


class CreditTransactionViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = CreditTransactionSerializer
    filterset_fields = ["type", "account"]

    def get_queryset(self):
        return CreditTransaction.objects.filter(
            account__in=visible_accounts(self.request.user)
        ).select_related("account", "from_account", "to_account", "created_by")


class MyCreditBalanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "credits": request.user.credits,
            "reserved_credits": request.user.reserved_credits,
            "available_credits": request.user.credits - request.user.reserved_credits,
        })


class AssignCreditsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if request.user.role not in (request.user.Role.SUPERADMIN, request.user.Role.RESELLER):
            return Response({"detail": "You cannot assign credits."}, status=status.HTTP_403_FORBIDDEN)

        serializer = AssignCreditsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        target = get_user_model().objects.filter(pk=serializer.validated_data["target_user_id"]).select_related("tenant").first()
        if not target:
            return Response({"detail": "Target user not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            assign_credits(
                actor=request.user, target=target,
                amount=serializer.validated_data["amount"], reference=serializer.validated_data.get("reference", ""),
                deduct=serializer.validated_data.get("deduct", True),
            )
        except InsufficientCreditsError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except CreditError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)

        target.refresh_from_db()
        log_event(
            request=request, action="credits.assigned", tenant=target.tenant, object_type="accounts.User",
            object_id=target.pk, metadata={
                "amount": serializer.validated_data["amount"],
                "deduct": serializer.validated_data.get("deduct", True),
            },
        )
        return Response(
            {"target_user_id": target.pk, "credits": target.credits, "reserved_credits": target.reserved_credits},
            status=status.HTTP_201_CREATED,
        )


class DeductCreditsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if request.user.role not in (request.user.Role.SUPERADMIN, request.user.Role.RESELLER):
            return Response({"detail": "You cannot deduct credits."}, status=status.HTTP_403_FORBIDDEN)

        serializer = DeductCreditsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        target = get_user_model().objects.filter(pk=serializer.validated_data["target_user_id"]).select_related("tenant").first()
        if not target:
            return Response({"detail": "Target user not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            deduct_credits(
                actor=request.user, target=target,
                amount=serializer.validated_data["amount"], reference=serializer.validated_data.get("reference", ""),
            )
        except InsufficientCreditsError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except CreditError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)

        target.refresh_from_db()
        log_event(
            request=request, action="credits.deducted", tenant=target.tenant, object_type="accounts.User",
            object_id=target.pk, metadata={"amount": serializer.validated_data["amount"]},
        )
        return Response(
            {"target_user_id": target.pk, "credits": target.credits, "reserved_credits": target.reserved_credits},
            status=status.HTTP_201_CREATED,
        )
