import csv

from django.contrib.auth import get_user_model
from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.audit.services import log_event
from apps.compliance.models import ConsentEvent
from apps.compliance.serializers import ConsentEventSerializer
from apps.compliance.services import record_consent
from core.csv_utils import EchoBuffer

from .models import Contact, ContactGroup, ContactImport, ContactSegment, ContactTag
from .segments import resolve_segment_queryset
from .serializers import (
    ContactGroupSerializer,
    ContactImportSerializer,
    ContactSegmentSerializer,
    ContactSerializer,
    ContactTagSerializer,
)
from .tasks import process_contact_import

# Same summary wording as every other bulk endpoint.
from core.bulk import past_tense


def _bulk_detail(name, succeeded, failed, requested):
    noun = "contact" if succeeded == 1 else "contacts"
    verb = past_tense(name or "update")
    if not succeeded:
        return f"No contact could be {verb}."
    if failed:
        return f"{succeeded} {noun} of {requested} {verb}; {failed} could not be."
    return f"{succeeded} {noun} {verb}."


class TenantScopedViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return self.queryset.filter(tenant=self.request.user.tenant)

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.user.tenant)


class ContactGroupViewSet(TenantScopedViewSet):
    queryset = ContactGroup.objects.all()
    serializer_class = ContactGroupSerializer
    search_fields = ["name"]


class ContactTagViewSet(TenantScopedViewSet):
    queryset = ContactTag.objects.all()
    serializer_class = ContactTagSerializer
    search_fields = ["name"]


class ContactViewSet(TenantScopedViewSet):
    queryset = Contact.objects.select_related("owner").prefetch_related("groups", "tags")
    serializer_class = ContactSerializer

    filterset_fields = ["subscribed", "country", "groups", "tags", "owner", "lifecycle_status"]
    search_fields = ["name", "phone", "email", "company"]
    ordering_fields = ["created_at", "name", "last_message_at", "lead_score", "last_inbound_at"]

    def get_queryset(self):
        """
        The contact list, plus the three CRM ways of narrowing it.

        `?smart=` picks one of the behavioural segments (crm.segments);
        `?band=` filters on HOT/WARM/COLD; `?q=` is the natural-language box,
        which falls back to an ordinary name/phone/email/company search for
        whatever it could not parse (crm.search). All three compose with each
        other and with the plain filters above, because a user who typed
        "hot leads" and then ticked an owner means both.
        """
        from apps.crm import search as crm_search
        from apps.crm import segments as crm_segments
        from apps.crm.scoring import HOT_THRESHOLD, WARM_THRESHOLD

        queryset = super().get_queryset()

        smart = self.request.query_params.get("smart")
        if smart:
            segment = crm_segments.resolve(self.request.user.tenant, smart)
            if segment is not None:
                queryset = queryset.filter(pk__in=segment.values("pk"))

        band = (self.request.query_params.get("band") or "").upper()
        if band == "HOT":
            queryset = queryset.filter(lead_score__gte=HOT_THRESHOLD)
        elif band == "WARM":
            queryset = queryset.filter(lead_score__gte=WARM_THRESHOLD, lead_score__lt=HOT_THRESHOLD)
        elif band == "COLD":
            queryset = queryset.filter(lead_score__lt=WARM_THRESHOLD)

        query = self.request.query_params.get("q")
        if query:
            queryset, applied = crm_search.apply(queryset, query, user=self.request.user)
            # Stashed for list() to echo back, so the UI can show which parts
            # of the sentence it actually understood.
            self._search_applied = applied

        return queryset

    def list(self, request, *args, **kwargs):
        self._search_applied = []
        response = super().list(request, *args, **kwargs)
        if request.query_params.get("q") and isinstance(response.data, dict):
            response.data["understood"] = getattr(self, "_search_applied", [])
        return response

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.user.tenant, created_by=self.request.user)

    def perform_destroy(self, instance):
        tenant, pk, phone = instance.tenant, instance.pk, instance.phone
        instance.delete()
        log_event(
            request=self.request, action="contact.deleted", tenant=tenant,
            object_type="contact", object_id=pk, metadata={"phone": phone},
        )

    @action(detail=True, methods=["post"])
    def unsubscribe(self, request, pk=None):
        contact = self.get_object()
        # Routed through compliance rather than flipping the flag here: the
        # flag is only half the record. Who changed it and when is the half
        # that gets asked about later -- see apps.compliance.models.ConsentEvent.
        record_consent(
            contact, ConsentEvent.Event.OPT_OUT, source=ConsentEvent.Source.AGENT,
            actor=request.user, detail="Unsubscribed from the contacts page.",
        )
        log_event(request=request, action="contact.unsubscribed", tenant=contact.tenant, object_type="contact", object_id=contact.pk)
        return Response(ContactSerializer(contact).data)

    @action(detail=True, methods=["post"])
    def resubscribe(self, request, pk=None):
        contact = self.get_object()
        record_consent(
            contact, ConsentEvent.Event.OPT_IN, source=ConsentEvent.Source.AGENT,
            actor=request.user, detail="Resubscribed from the contacts page.",
        )
        log_event(request=request, action="contact.resubscribed", tenant=contact.tenant, object_type="contact", object_id=contact.pk)
        return Response(ContactSerializer(contact).data)

    @action(detail=True, methods=["get"], url_path="consent-history")
    def consent_history(self, request, pk=None):
        """Every consent decision recorded for this contact, newest first."""
        contact = self.get_object()
        return Response(ConsentEventSerializer(contact.consent_events.all()[:100], many=True).data)

    @action(detail=True, methods=["get"])
    def profile(self, request, pk=None):
        """
        Everything the one customer screen shows, in a single request.

        The alternative -- a request each for deals, tasks, notes, threads and
        totals -- is five round trips on open and five chances to render half
        a customer. See apps.crm.services.contact_profile.
        """
        from apps.crm.serializers import ContactNoteSerializer, DealSerializer, TaskSerializer
        from apps.crm.services import contact_profile
        from apps.inbox.serializers import ConversationSerializer

        contact = self.get_object()
        profile = contact_profile(contact)
        return Response({
            "contact": ContactSerializer(contact).data,
            "deals": DealSerializer(profile["deals"], many=True).data,
            "tasks": TaskSerializer(profile["tasks"], many=True).data,
            "notes": ContactNoteSerializer(profile["notes"], many=True).data,
            "conversations": ConversationSerializer(profile["conversations"], many=True).data,
            "open_deal_value": profile["open_deal_value"],
            "won_deal_value": profile["won_deal_value"],
            "next_action": profile["next_action"],
            "next_action_at": profile["next_action_at"],
        })

    @action(detail=True, methods=["get"])
    def duplicates(self, request, pk=None):
        """
        Other records that look like the same person.

        The phone number is the primary key of a WhatsApp contact and is
        already unique per tenant, so an exact duplicate cannot exist -- what
        can is the same person saved twice with the number written differently
        (a missing country code, a leading zero). Matching on the last nine
        digits catches those without pulling in unrelated numbers.
        """
        contact = self.get_object()
        tail = contact.phone[-9:]
        candidates = (
            self.get_queryset()
            .exclude(pk=contact.pk)
            .filter(phone__endswith=tail)
            .select_related("owner")[:20]
        )
        matches = list(candidates)
        if contact.email:
            by_email = self.get_queryset().exclude(pk=contact.pk).filter(email__iexact=contact.email)[:20]
            seen = {item.pk for item in matches}
            matches += [item for item in by_email if item.pk not in seen]
        return Response({"results": ContactSerializer(matches, many=True).data})

    @action(detail=True, methods=["post"])
    def merge(self, request, pk=None):
        """
        Fold another contact into this one. This one survives.

        Never destructive of history: messages, conversations, deals, tasks,
        notes and timeline entries all move across before the duplicate row is
        removed, and an opt-out on either record binds the survivor.
        """
        from apps.crm.services import merge_contacts

        primary = self.get_object()
        duplicate = self.get_queryset().filter(pk=request.data.get("duplicate_id")).first()
        if duplicate is None:
            return Response({"detail": "Choose one of your own contacts to merge in."}, status=status.HTTP_400_BAD_REQUEST)
        if duplicate.pk == primary.pk:
            return Response({"detail": "A contact cannot be merged into itself."}, status=status.HTTP_400_BAD_REQUEST)

        merged_phone = duplicate.phone
        merge_contacts(primary, duplicate, actor=request.user)
        primary.refresh_from_db()
        log_event(
            request=request, action="contact.merged", tenant=request.user.tenant,
            object_type="contact", object_id=primary.pk, metadata={"merged_phone": merged_phone},
        )
        return Response(ContactSerializer(primary).data)

    @action(detail=False, methods=["post"])
    def bulk(self, request):
        # `ids` is the name every other bulk endpoint uses (see core.bulk);
        # `contact_ids` predates them and is still accepted so existing callers
        # keep working.
        contact_ids = request.data.get("ids") or request.data.get("contact_ids") or []
        bulk_action = request.data.get("action")
        if not contact_ids:
            return Response({"detail": "Select at least one contact."}, status=status.HTTP_400_BAD_REQUEST)
        queryset = self.get_queryset().filter(id__in=contact_ids)

        if bulk_action == "delete":
            count = queryset.count()
            queryset.delete()
        elif bulk_action in ("unsubscribe", "resubscribe"):
            # One row per contact rather than a single UPDATE: a bulk opt-out
            # is still one consent decision per person, and the log has to be
            # able to say so for each of them individually.
            event = (
                ConsentEvent.Event.OPT_OUT if bulk_action == "unsubscribe" else ConsentEvent.Event.OPT_IN
            )
            count = 0
            for contact in queryset:
                record_consent(
                    contact, event, source=ConsentEvent.Source.AGENT, actor=request.user,
                    detail=f"Bulk {bulk_action} from the contacts page.",
                )
                count += 1
        elif bulk_action == "assign_owner":
            # Reassigning a book of business is a routine Monday-morning
            # action, so it is a bulk one. Each contact still gets its own
            # timeline entry -- "who owns this and since when" is exactly the
            # question the timeline exists to answer.
            from apps.crm.services import assign_owner

            owner = None
            owner_id = request.data.get("owner_id")
            if owner_id:
                owner = get_user_model().objects.filter(pk=owner_id, tenant=self.request.user.tenant).first()
                if owner is None:
                    return Response({"detail": "Choose someone on your team."}, status=status.HTTP_400_BAD_REQUEST)
            count = 0
            for contact in queryset.select_related("owner"):
                assign_owner(contact, owner, actor=request.user)
                count += 1
        elif bulk_action == "set_status":
            from apps.crm.lifecycle import set_status

            new_status = request.data.get("lifecycle_status")
            if new_status not in Contact.Lifecycle.values:
                return Response({"detail": "Unknown status."}, status=status.HTTP_400_BAD_REQUEST)
            count = 0
            for contact in queryset:
                set_status(contact, new_status, actor=request.user, reason="set in bulk")
                count += 1
        elif bulk_action in ("add_tag", "remove_tag", "add_group", "remove_group"):
            relation, verb = ("tags", bulk_action.split("_")[0]) if "tag" in bulk_action else ("groups", bulk_action.split("_")[0])
            model = ContactTag if relation == "tags" else ContactGroup
            item_id = request.data.get("tag_id" if relation == "tags" else "group_id")
            try:
                item = model.objects.get(tenant=self.request.user.tenant, id=item_id)
            except model.DoesNotExist:
                return Response({"detail": "Item not found."}, status=status.HTTP_400_BAD_REQUEST)
            count = queryset.count()
            if verb == "add":
                item.contacts.add(*queryset)
            else:
                item.contacts.remove(*queryset)
        else:
            return Response({"detail": "Unsupported action."}, status=status.HTTP_400_BAD_REQUEST)

        log_event(
            request=request, action="contact.bulk_action", tenant=self.request.user.tenant,
            object_type="contact", metadata={"bulk_action": bulk_action, "count": count},
        )
        # Contacts are updated as one queryset, so the only "failure" possible
        # is an id that is not this tenant's (or already gone). Reported the
        # same way core.bulk reports it, so the UI has one response shape.
        missing = len(contact_ids) - count
        return Response({
            "success": True,
            "action": bulk_action,
            "affected": count,
            "requested": len(contact_ids),
            "succeeded": count,
            "failed": max(missing, 0),
            "errors": [],
            "detail": _bulk_detail(bulk_action, count, max(missing, 0), len(contact_ids)),
        })

    @action(detail=False, methods=["get"])
    def export(self, request):
        queryset = self.filter_queryset(self.get_queryset()).prefetch_related("tags", "groups")
        # ?ids=1,2,3 exports just the ticked rows; without it the export keeps
        # covering whatever the current search and filters select.
        selected = [value for value in request.query_params.get("ids", "").split(",") if value.strip()]
        if selected:
            queryset = queryset.filter(pk__in=selected)

        def rows():
            buffer = EchoBuffer()
            writer = csv.writer(buffer)
            yield writer.writerow(["name", "phone", "email", "country", "subscribed", "tags", "groups"])
            for contact in queryset.iterator(chunk_size=200):
                yield writer.writerow([
                    contact.name,
                    contact.phone,
                    contact.email,
                    contact.country,
                    contact.subscribed,
                    ",".join(t.name for t in contact.tags.all()),
                    ",".join(g.name for g in contact.groups.all()),
                ])

        response = StreamingHttpResponse(rows(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="contacts.csv"'
        return response


class ContactImportViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ContactImportSerializer
    http_method_names = ["get", "post", "head", "options"]

    #: Rows read by the preview. Enough to be representative of a large file
    #: without holding one in memory to answer a question about its shape.
    PREVIEW_ROWS = 2000

    def get_queryset(self):
        return ContactImport.objects.filter(tenant=self.request.user.tenant)

    @action(detail=False, methods=["post"])
    def preview(self, request):
        """
        Read the file, say what is in it, import nothing.

        This is what replaces the column-mapping screen: it reports which
        header it thinks is which field, and how the rows split into new,
        already-here, and unusable -- so the only decision left is whether to
        go ahead. Nothing is written; the real import is a separate POST.
        """
        import csv as _csv
        import io as _io

        from .mapping import detect_columns, row_value
        from .utils import normalize_phone

        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "Attach a .csv file."}, status=status.HTTP_400_BAD_REQUEST)
        if not upload.name.lower().endswith(".csv"):
            return Response({"detail": "Only .csv files are supported."}, status=status.HTTP_400_BAD_REQUEST)
        if upload.size > 10 * 1024 * 1024:
            return Response({"detail": "File must be smaller than 10MB."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            raw = upload.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return Response(
                {"detail": "That file is not UTF-8 text. Re-export it as CSV UTF-8."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reader = _csv.DictReader(_io.StringIO(raw))
        headers = list(reader.fieldnames or [])
        mapping = detect_columns(headers)

        total = invalid = 0
        seen, duplicates_in_file = set(), 0
        sample = []
        for row in reader:
            if total >= self.PREVIEW_ROWS:
                break
            total += 1
            raw_phone = row_value(row, mapping, "phone")
            try:
                phone = normalize_phone(raw_phone)
            except ValueError:
                invalid += 1
                continue
            if phone in seen:
                duplicates_in_file += 1
                continue
            seen.add(phone)
            if len(sample) < 5:
                sample.append({
                    "phone": phone,
                    "name": row_value(row, mapping, "name"),
                    "email": row_value(row, mapping, "email"),
                    "company": row_value(row, mapping, "company"),
                })

        existing = Contact.objects.filter(tenant=request.user.tenant, phone__in=seen).count() if seen else 0

        return Response({
            "headers": headers,
            "mapping": mapping,
            "unmapped_headers": [header for header in headers if header not in mapping.values()],
            "rows_scanned": total,
            "truncated": total >= self.PREVIEW_ROWS,
            "valid": len(seen),
            "new": max(len(seen) - existing, 0),
            "existing": existing,
            "invalid": invalid,
            "duplicates_in_file": duplicates_in_file,
            "sample": sample,
        })

    def perform_create(self, serializer):
        contact_import = serializer.save(tenant=self.request.user.tenant, created_by=self.request.user)
        process_contact_import.delay(contact_import.pk)
        log_event(
            request=self.request, action="contact.import_started", tenant=self.request.user.tenant,
            object_type="contact_import", object_id=contact_import.pk,
        )


class ContactSegmentViewSet(TenantScopedViewSet):
    queryset = ContactSegment.objects.all()
    serializer_class = ContactSegmentSerializer
    search_fields = ["name"]

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.user.tenant, created_by=self.request.user)

    @action(detail=True, methods=["get"])
    def contacts(self, request, pk=None):
        segment = self.get_object()
        queryset = resolve_segment_queryset(request.user.tenant, segment.filters)
        page = self.paginate_queryset(queryset)
        serializer = ContactSerializer(page if page is not None else queryset, many=True)
        return self.get_paginated_response(serializer.data) if page is not None else Response(serializer.data)
