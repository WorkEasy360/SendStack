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
from apps.subscriptions.services import check_quota, quota_guard
from core.csv_utils import EchoBuffer, csv_safe_row

from .models import Contact, ContactGroup, ContactImport, ContactSegment, ContactTag
from .segments import resolve_segment_queryset
from .serializers import (
    ContactGroupSerializer,
    ContactImportSerializer,
    ContactSegmentSerializer,
    ContactSerializer,
    ContactTagSerializer,
    PasteImportSerializer,
    PasteParseSerializer,
)
from .tasks import process_contact_import

# Same summary wording as every other bulk endpoint.
from core.bulk import past_tense


def _paste_summary(created, updated, unchanged, errors) -> str:
    """
    One sentence saying what the import did.

    Every outcome is named, including the ones that are not failures: "40
    imported" against a paste of fifty leaves somebody wondering about the
    other ten, and "8 were already in your contacts" answers it.
    """
    parts = []
    if created:
        parts.append(f"{created:,} contact{'' if created == 1 else 's'} added")
    if updated:
        parts.append(f"{updated:,} updated")
    if unchanged:
        parts.append(f"{unchanged:,} already in your contacts")
    if errors:
        parts.append(f"{len(errors):,} could not be imported")
    if not parts:
        return "Nothing was imported."
    return ", ".join(parts) + "."


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

    filterset_fields = [
        "subscribed", "country", "groups", "tags", "owner", "lifecycle_status",
        # "Show me everyone I have no consent on file for" is the first
        # question a customer asks when turning consent enforcement on.
        "marketing_consent",
        # "Show me the numbers that are actually on WhatsApp", once the number
        # filter has been run over the list.
        "whatsapp_status",
    ]
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
        # The plan's contact ceiling, held across the create so two requests
        # cannot both take the last slot. Every other way a contact can be
        # created is guarded too -- the CSV import in apps/contacts/tasks.py,
        # and nowhere else writes one except the two inbound paths, which are
        # deliberately exempt (see docs/ENTITLEMENTS.md: refusing to record
        # somebody who has just messaged you means losing their message).
        with quota_guard(self.request.user.tenant_id, "contacts"):
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

    @action(detail=True, methods=["post"], url_path="set-consent")
    def set_consent(self, request, pk=None):
        """
        Record whether this contact has agreed to marketing, and where that came from.

        Deliberately separate from `resubscribe`. Recording "they ticked the box
        on our website in March" is a statement about evidence; clearing an
        opt-out is a statement that the person changed their mind. Merging the
        two would let an import quietly undo a STOP, which is exactly the thing
        the precedence rule exists to prevent -- so this endpoint never touches
        `subscribed`, and says so if asked to grant consent to somebody who has
        opted out.
        """
        from apps.compliance.services import set_marketing_consent

        contact = self.get_object()
        granted = request.data.get("granted")
        if granted is None:
            return Response({"detail": "Say whether consent was granted."}, status=status.HTTP_400_BAD_REQUEST)
        granted = str(granted).lower() in ("1", "true", "yes", "on")

        source = (request.data.get("source") or "").upper()
        if source not in Contact.ConsentSource.values:
            return Response(
                {"detail": f"Choose a consent source: {', '.join(Contact.ConsentSource.values)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        set_marketing_consent(
            contact, granted, source=source, actor=request.user,
            reference=request.data.get("reference") or "",
            detail=request.data.get("detail") or "",
        )
        contact.refresh_from_db()
        log_event(
            request=request, action="contact.consent_recorded", tenant=contact.tenant,
            object_type="contact", object_id=contact.pk,
            metadata={"granted": granted, "source": source},
        )
        payload = ContactSerializer(contact).data
        if granted and not contact.subscribed:
            # Said plainly rather than silently doing half the job. The consent
            # is on file, and the opt-out still wins -- which is the correct
            # outcome and a surprising one, so it is worth a sentence.
            payload["warning"] = (
                "Consent was recorded, but this contact has unsubscribed and will still not be "
                "sent marketing. An opt-out can only be reversed by the contact."
            )
        return Response(payload)

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
        elif bulk_action in ("grant_consent", "deny_consent"):
            # The explicit workflow for imported contacts: a customer who knows
            # where a list came from marks it, once, rather than the importer
            # guessing. Every row still gets its own ConsentEvent, because the
            # source is per-contact evidence and a single summary row would not
            # survive one of them being questioned.
            from apps.compliance.services import set_marketing_consent

            source = (request.data.get("source") or "").upper()
            if source not in Contact.ConsentSource.values:
                return Response(
                    {"detail": f"Choose a consent source: {', '.join(Contact.ConsentSource.values)}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            granted = bulk_action == "grant_consent"
            reference = request.data.get("reference") or ""
            count = 0
            for contact in queryset:
                set_marketing_consent(
                    contact, granted, source=source, actor=request.user, reference=reference,
                    detail=f"Bulk {'consent' if granted else 'refusal'} recorded from the contacts page.",
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
            # The consent columns travel with the contact. A customer who
            # leaves must be able to take the evidence with them, and a
            # customer answering a complaint needs it in the same file as the
            # number it is about.
            yield writer.writerow([
                "name", "phone", "email", "country", "subscribed", "unsubscribed_at",
                "marketing_consent", "consent_source", "consent_at", "consent_reference",
                "tags", "groups",
            ])
            for contact in queryset.iterator(chunk_size=200):
                # csv_safe_row, not writerow directly: a contact's name, email
                # or company is whatever a stranger typed at this business, and
                # a cell starting with "=" is a formula the moment somebody
                # opens the download. See core.csv_utils.
                yield writer.writerow(csv_safe_row([
                    contact.name,
                    contact.phone,
                    contact.email,
                    contact.country,
                    contact.subscribed,
                    contact.unsubscribed_at.isoformat() if contact.unsubscribed_at else "",
                    contact.marketing_consent,
                    contact.consent_source,
                    contact.consent_at.isoformat() if contact.consent_at else "",
                    contact.consent_reference,
                    ",".join(t.name for t in contact.tags.all()),
                    ",".join(g.name for g in contact.groups.all()),
                ]))

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
        from .mapping import detect_columns, row_value
        from .readers import SUPPORTED_EXTENSIONS, UnreadableFile, extension_of, open_rows
        from .utils import normalize_contact_phone

        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "Attach a .csv or .xlsx file."}, status=status.HTTP_400_BAD_REQUEST)
        if extension_of(upload.name) not in SUPPORTED_EXTENSIONS:
            return Response(
                {"detail": "Only .csv and .xlsx files are supported. Save an .xls workbook as .xlsx first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if upload.size > 10 * 1024 * 1024:
            return Response({"detail": "File must be smaller than 10MB."}, status=status.HTTP_400_BAD_REQUEST)

        total = invalid = 0
        seen, duplicates_in_file = set(), 0
        sample = []
        try:
            # The same reader the import task uses, so the preview describes
            # exactly the rows the import will act on -- for a workbook as
            # much as for a CSV.
            headers, reader = open_rows(upload, upload.name)
            mapping = detect_columns(headers)
            for row in reader:
                if total >= self.PREVIEW_ROWS:
                    break
                total += 1
                raw_phone = row_value(row, mapping, "phone")
                try:
                    phone = normalize_contact_phone(raw_phone)
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
        except UnreadableFile as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        existing = Contact.objects.filter(tenant=request.user.tenant, phone__in=seen).count() if seen else 0
        genuinely_new = max(len(seen) - existing, 0)

        # The quota is measured against the contacts this file would actually
        # add -- after normalisation, after the duplicates inside the file,
        # and after the ones already on the account. A 5,000-row export of a
        # list already imported adds nothing and must not be refused for
        # being 5,000 rows long.
        within_quota, quota_reason = check_quota(request.user.tenant_id, "contacts", genuinely_new)

        return Response({
            "headers": headers,
            "mapping": mapping,
            "unmapped_headers": [header for header in headers if header not in mapping.values()],
            "rows_scanned": total,
            "truncated": total >= self.PREVIEW_ROWS,
            "valid": len(seen),
            "new": genuinely_new,
            "existing": existing,
            "invalid": invalid,
            "duplicates_in_file": duplicates_in_file,
            "sample": sample,
            # So the screen can say what will happen before anybody presses
            # Import, rather than after half the file is in.
            "within_quota": within_quota,
            "quota_warning": "" if within_quota else quota_reason,
        })

    @action(detail=False, methods=["post"])
    def parse(self, request):
        """
        Classify a pasted list. **Writes nothing.**

        This is the whole of what makes the paste importer safe to use on a
        list somebody has not checked: every line comes back with a status and
        the number it normalised to, so the four things that go wrong -- a
        typo, a repeat, a number already on the account, and somebody who has
        unsubscribed -- are visible before anything is created rather than
        discovered in a campaign report afterwards.
        """
        from .paste import parse as parse_paste
        from .paste import summarise

        payload = PasteParseSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        # Parsed once with no account context, so the numbers are known before
        # the database is asked about any of them -- one query for the whole
        # paste instead of one per line.
        rows = parse_paste(
            payload.validated_data["text"],
            default_country_code=payload.validated_data.get("default_country_code", ""),
        )
        numbers = {row["phone"] for row in rows if row["phone"]}

        existing, unsubscribed = set(), set()
        if numbers:
            for phone, subscribed in Contact.objects.filter(
                tenant=request.user.tenant, phone__in=numbers,
            ).values_list("phone", "subscribed"):
                existing.add(phone)
                if not subscribed:
                    unsubscribed.add(phone)

        rows = parse_paste(
            payload.validated_data["text"],
            default_country_code=payload.validated_data.get("default_country_code", ""),
            existing=existing,
            unsubscribed=unsubscribed,
        )
        summary = summarise(rows)

        # Measured against the contacts this paste would actually add, exactly
        # as the CSV preview measures its file: re-pasting a list already on
        # the account adds nobody and must not be refused for being long.
        within_quota, quota_reason = check_quota(request.user.tenant_id, "contacts", summary["new"])

        return Response({
            "rows": rows,
            "summary": summary,
            "within_quota": within_quota,
            "quota_warning": "" if within_quota else quota_reason,
        })

    @action(detail=False, methods=["post"], url_path="paste")
    def paste_import(self, request):
        """
        Create the reviewed rows.

        Synchronous, unlike the CSV import, and deliberately: a paste is
        bounded (see paste.MAX_LINES), the reviewer is watching, and handing
        them a job id to poll for fifty contacts would be worse than making
        them wait a second.

        Every number is re-normalised here rather than trusted from the review
        table -- the browser's parse is a convenience, and this is the
        authority.

        The response carries the resolved contact ids as well as the counts,
        because a paste is not always made in order to *have* the contacts: the
        campaign wizard pastes a list in order to send to it, and needs the ids
        of everybody the paste resolved to -- the ones already on the account
        as much as the ones it created.
        """
        from .paste import MAX_LINES
        from .upsert import ContactQuotaReached, upsert_contact
        from .utils import normalize_phone

        payload = PasteImportSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        tenant = request.user.tenant
        groups = list(ContactGroup.objects.filter(tenant=tenant, id__in=data["group_ids"]))
        tags = list(ContactTag.objects.filter(tenant=tenant, id__in=data["tag_ids"]))

        created = updated = unchanged = unsubscribed = 0
        errors = []
        seen: set = set()
        # Every contact this paste resolved to, in the order it was pasted.
        contact_ids = []

        for index, row in enumerate(data["rows"][:MAX_LINES], start=1):
            try:
                phone = normalize_phone(row["phone"])
            except ValueError:
                errors.append({"row": index, "phone": row["phone"], "error": "Not a valid phone number."})
                continue

            # A repeat inside one request is skipped rather than treated as an
            # update: the reviewer may have removed some rows but not all of a
            # duplicate pair, and importing the same number twice would count
            # it as one created and one unchanged, which reads as a bug.
            if phone in seen:
                continue
            seen.add(phone)

            try:
                contact, outcome = upsert_contact(
                    tenant=tenant,
                    phone=phone,
                    defaults={"name": row.get("name", ""), "created_by": request.user},
                    groups=groups,
                    tags=tags,
                    consent_source=data["consent_source"],
                    consent_reference=data["consent_reference"],
                    actor=request.user,
                    consent_detail="Declared on a pasted import.",
                )
            except ContactQuotaReached as exc:
                # Everything already created stays created, and the response
                # says exactly where it stopped -- the same contract the CSV
                # importer's report makes.
                errors.append({"row": index, "phone": phone, "error": str(exc.detail)})
                break

            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1
            else:
                unchanged += 1

            contact_ids.append(contact.pk)
            # Read off the contact rather than the review table, because this
            # is the one status a caller must not get wrong: an unsubscribed
            # row imports perfectly happily and is still never messaged, and a
            # caller that thinks it just built an audience of fifty needs to
            # be told that three of them are not in it.
            if not contact.subscribed:
                unsubscribed += 1

        log_event(
            request=request, action="contact.paste_imported", tenant=tenant,
            object_type="contact",
            metadata={"created": created, "updated": updated, "unchanged": unchanged, "errors": len(errors)},
        )

        return Response(
            {
                "created": created,
                "updated": updated,
                "unchanged": unchanged,
                "unsubscribed": unsubscribed,
                "contact_ids": contact_ids,
                "errors": errors,
                "detail": _paste_summary(created, updated, unchanged, errors),
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def perform_create(self, serializer):
        # Deliberately *not* refused up front, even for an account that is
        # already full. How many contacts a file adds cannot be known without
        # parsing it, and a re-import of a list already on the account adds
        # none at all -- refusing that because the account is at its ceiling
        # would reject the very import that was always going to be free.
        # `preview` above answers the question properly for the screen that
        # asks it first, and the task enforces the ceiling per row and reports
        # exactly which row it stopped at.
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
