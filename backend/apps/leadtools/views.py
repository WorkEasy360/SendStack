import csv

from django.http import StreamingHttpResponse
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import log_event
from apps.subscriptions.permissions import RequiresFeature
from apps.whatsapp.providers.base import ProviderError
from core.csv_utils import EchoBuffer, csv_safe_row

from . import services
from .models import GroupGrab, NumberCheck, NumberCheckEntry
from .serializers import (
    ContactImportSummarySerializer,
    GroupGrabCreateSerializer,
    GroupGrabSerializer,
    NumberCheckCreateSerializer,
    NumberCheckEntrySerializer,
    NumberCheckSerializer,
    SaveEntriesSerializer,
)


def _provider_failure(exc: ProviderError) -> Response:
    """
    WhatsApp, or the bridge in front of it, said no.

    409 when the number simply is not connected -- the fix is on the Devices
    page -- and 502 for everything else, because the sentence to show is the
    provider's and the fix is usually "try again in a minute".
    """
    code = exc.code or "provider_error"
    http_status = status.HTTP_409_CONFLICT if code in ("not_connected", "no_session") else status.HTTP_502_BAD_GATEWAY
    return Response({"detail": exc.message, "code": code}, status=http_status)


class NumberCheckViewSet(
    mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet,
):
    """
    The number filter: which of a list of numbers are on WhatsApp.

    A check is created, runs on the worker, and is polled; its rows are read
    through ``entries`` (paged, filterable by result), exported as CSV, or
    saved into a contact group -- which is where "filter my list before I
    send to it" actually ends.
    """

    permission_classes = [IsAuthenticated, RequiresFeature.for_feature("number_filter")]
    serializer_class = NumberCheckSerializer

    def get_queryset(self):
        return NumberCheck.objects.filter(
            tenant=self.request.user.tenant,
        ).select_related("whatsapp_account", "contact_group")

    def create(self, request, *args, **kwargs):
        payload = NumberCheckCreateSerializer(data=request.data, context={"request": request})
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        try:
            account = services.usable_account(request.user.tenant, data["whatsapp_account"], "number_check")
            check = services.start_number_check(
                user=request.user, account=account, source=data["source"], text=data.get("text", ""),
                default_country_code=data.get("default_country_code", ""), contact_group=data.get("contact_group"),
            )
        except services.LeadToolError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        log_event(
            request=request, action="leadtools.number_check_started", tenant=request.user.tenant,
            object_type="number_check", object_id=check.pk,
            metadata={"source": check.source, "total": check.total, "whatsapp_account": account.pk},
        )
        return Response(NumberCheckSerializer(check).data, status=status.HTTP_201_CREATED)

    def _entries(self, check, request):
        queryset = check.entries.select_related("contact").order_by("pk")
        result = request.query_params.get("result")
        if result in NumberCheckEntry.Result.values:
            queryset = queryset.filter(result=result)
        return queryset

    @action(detail=True, methods=["get"])
    def entries(self, request, pk=None):
        check = self.get_object()
        page = self.paginate_queryset(self._entries(check, request))
        return self.get_paginated_response(NumberCheckEntrySerializer(page, many=True).data)

    @action(detail=True, methods=["get"])
    def export(self, request, pk=None):
        """The rows as CSV -- all of them, or just one result via ``?result=``."""
        check = self.get_object()
        queryset = self._entries(check, request)

        def rows():
            buffer = EchoBuffer()
            writer = csv.writer(buffer)
            yield writer.writerow(["phone", "name", "result", "checked_at"])
            for entry in queryset.iterator(chunk_size=1000):
                # csv_safe_row: a contact's name is theirs to choose, and a
                # spreadsheet would run "=..." as a formula. See core.csv_utils.
                yield writer.writerow(csv_safe_row([
                    entry.phone,
                    entry.contact.name if entry.contact else "",
                    entry.get_result_display(),
                    entry.checked_at.isoformat() if entry.checked_at else "",
                ]))

        response = StreamingHttpResponse(rows(), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="number-check-{check.pk}.csv"'
        return response

    @action(detail=True, methods=["post"])
    def save(self, request, pk=None):
        """Put the numbers with one result into a contact group, through the importer."""
        check = self.get_object()
        payload = SaveEntriesSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            contact_import, group = services.save_check_entries(
                check, user=request.user, result=payload.validated_data["result"],
                group_name=payload.validated_data["group_name"],
            )
        except services.LeadToolError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        log_event(
            request=request, action="leadtools.number_check_saved", tenant=request.user.tenant,
            object_type="number_check", object_id=check.pk,
            metadata={"result": payload.validated_data["result"], "contact_group": group.pk},
        )
        return Response({
            "contact_import": ContactImportSummarySerializer(contact_import).data,
            "contact_group": {"id": group.pk, "name": group.name},
        }, status=status.HTTP_201_CREATED)


class WhatsAppGroupsView(APIView):
    """``GET /leadtools/groups/?account=<id>`` -- every group that number is in."""

    permission_classes = [IsAuthenticated, RequiresFeature.for_feature("group_grabber")]

    def get(self, request):
        account_id = request.query_params.get("account")
        if not account_id or not str(account_id).isdigit():
            return Response({"detail": "Say which number to read groups from (?account=)."},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            account = services.usable_account(request.user.tenant, int(account_id), "groups")
            groups = services.list_groups(account)
        except services.LeadToolError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ProviderError as exc:
            return _provider_failure(exc)
        return Response({"account": account.pk, "groups": groups})


class GroupGrabViewSet(
    mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet,
):
    """The group grabber: members of chosen groups, into a contact group."""

    permission_classes = [IsAuthenticated, RequiresFeature.for_feature("group_grabber")]
    serializer_class = GroupGrabSerializer

    def get_queryset(self):
        return GroupGrab.objects.filter(
            tenant=self.request.user.tenant,
        ).select_related("whatsapp_account", "contact_group", "contact_import")

    def create(self, request, *args, **kwargs):
        payload = GroupGrabCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        try:
            account = services.usable_account(request.user.tenant, data["whatsapp_account"], "groups")
            grab = services.grab_groups(
                user=request.user, account=account, group_ids=data["group_ids"],
                contact_group_name=data.get("contact_group_name", ""),
            )
        except services.LeadToolError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ProviderError as exc:
            return _provider_failure(exc)
        log_event(
            request=request, action="leadtools.group_grabbed", tenant=request.user.tenant,
            object_type="group_grab", object_id=grab.pk,
            metadata={
                "groups": [group["id"] for group in grab.groups],
                "members_found": grab.members_found, "whatsapp_account": account.pk,
            },
        )
        return Response(GroupGrabSerializer(grab).data, status=status.HTTP_201_CREATED)
