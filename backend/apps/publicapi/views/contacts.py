"""
Contacts, read and written through the same upsert the importers use.

Writing goes through `apps.contacts.upsert.upsert_contact` rather than
`Contact.objects.create`, which buys three behaviours this endpoint would
otherwise have to reimplement and get subtly different: the plan's contact
ceiling is charged under the tenant's own lock, an existing contact has gaps
filled and never data overwritten, and consent handling stays in one place.
"""

from apps.contacts.models import Contact
from apps.contacts.upsert import ContactQuotaReached, upsert_contact
from apps.contacts.utils import normalize_phone
from rest_framework import serializers

from .. import scopes
from ..errors import INVALID_RECIPIENT, NOT_FOUND, QUOTA_EXCEEDED, PublicAPIError
from ..serializers import CONTACT_PREFIX, parse_public_id, serialize_contact
from .base import PublicAPIView


class ContactWriteSerializer(serializers.Serializer):
    phone = serializers.CharField()
    name = serializers.CharField(required=False, allow_blank=True, default="")
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    company = serializers.CharField(required=False, allow_blank=True, default="", max_length=150)
    custom_fields = serializers.DictField(required=False, default=dict)


class ContactListView(PublicAPIView):
    """``GET /contacts`` and ``POST /contacts``."""

    required_scopes = [scopes.CONTACTS_READ]
    method_scopes = {"POST": [scopes.CONTACTS_WRITE]}

    def get(self, request):
        queryset = Contact.objects.filter(tenant=request.auth.tenant).order_by("-created_at")

        phone = request.query_params.get("phone")
        if phone:
            # Normalised before matching, so a caller searching "9198..." finds
            # the row stored as "+9198...". A lookup that only matched the
            # exact stored form would make this endpoint useless for the thing
            # it is mostly used for: "do I already know this person?"
            try:
                queryset = queryset.filter(phone=normalize_phone(phone))
            except ValueError:
                queryset = queryset.none()

        search = request.query_params.get("search")
        if search:
            queryset = queryset.filter(name__icontains=search.strip())

        subscribed = request.query_params.get("subscribed")
        if subscribed is not None:
            queryset = queryset.filter(subscribed=subscribed.strip().lower() in ("1", "true", "yes"))

        return self.ok(self.paginate(queryset, request, serialize_contact))

    def post(self, request):
        serializer = ContactWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            phone = normalize_phone(data["phone"])
        except ValueError as exc:
            raise PublicAPIError(INVALID_RECIPIENT, str(exc)) from exc

        try:
            contact, outcome = upsert_contact(
                tenant=request.auth.tenant,
                phone=phone,
                defaults={
                    "name": data.get("name", ""),
                    "email": data.get("email", ""),
                    "company": data.get("company", ""),
                    "created_by": request.auth.owner,
                },
                custom_fields=data.get("custom_fields") or {},
                actor=request.auth.owner,
            )
        except ContactQuotaReached as exc:
            raise PublicAPIError(QUOTA_EXCEEDED, str(exc.detail), status_code=402) from exc

        payload = serialize_contact(contact)
        # Which of the three things happened. An integration syncing a CRM
        # needs this to report "42 added, 3 updated" without diffing.
        payload["outcome"] = outcome
        return self.ok(payload, status_code=201 if outcome == "created" else 200)


class ContactDetailView(PublicAPIView):
    """``GET /contacts/{id}``."""

    required_scopes = [scopes.CONTACTS_READ]

    def get(self, request, contact_id):
        pk = parse_public_id(contact_id, CONTACT_PREFIX)
        contact = None
        if pk is not None:
            contact = Contact.objects.filter(tenant=request.auth.tenant, pk=pk).first()
        if contact is None:
            raise PublicAPIError(
                NOT_FOUND, f"No contact '{contact_id}' in this workspace.", status_code=404,
            )
        return self.ok(serialize_contact(contact))
