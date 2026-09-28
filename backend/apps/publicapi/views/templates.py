"""Templates, as an external integration sees them."""

from django.db import IntegrityError, transaction

from apps.templates_app.models import MessageTemplate

from .. import scopes
from ..errors import INVALID_REQUEST, INVALID_TEMPLATE, PublicAPIError
from ..serializers import TemplateWriteSerializer, serialize_template
from ..services import resolve_template
from .base import PublicAPIView


class TemplateListView(PublicAPIView):
    """``GET /templates`` and ``POST /templates``."""

    # Read to list, write to create. One `required_scopes` for both would
    # either lock read-only keys out of listing or let them create templates.
    required_scopes = [scopes.TEMPLATES_READ]
    method_scopes = {"POST": [scopes.TEMPLATES_WRITE]}

    def get(self, request):
        queryset = MessageTemplate.objects.filter(tenant=request.auth.tenant).order_by("name")

        active = request.query_params.get("active")
        if active is not None:
            queryset = queryset.filter(is_active=active.strip().lower() in ("1", "true", "yes"))

        search = request.query_params.get("search")
        if search:
            queryset = queryset.filter(name__icontains=search.strip())

        return self.ok(self.paginate(queryset, request, serialize_template))

    def post(self, request):
        serializer = TemplateWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            # The savepoint is what makes catching IntegrityError safe. Without
            # it, PostgreSQL leaves the whole transaction in a failed state and
            # every later query in this request raises -- including the request
            # log write, so the one call somebody wants to look up afterwards is
            # the one that goes unrecorded.
            with transaction.atomic():
                template = serializer.save(tenant=request.auth.tenant, created_by=request.auth.owner)
        except IntegrityError as exc:
            # MessageTemplate has a (tenant, name) uniqueness constraint. Named
            # explicitly rather than surfacing as a 500, because "you already
            # have a template called that" is a thing the caller can fix.
            raise PublicAPIError(
                INVALID_REQUEST,
                f"A template named '{serializer.validated_data.get('name')}' already exists.",
                status_code=409,
            ) from exc
        return self.ok(serialize_template(template), status_code=201)


class TemplateDetailView(PublicAPIView):
    """``GET``, ``PUT`` and ``DELETE`` on ``/templates/{id}``."""

    required_scopes = [scopes.TEMPLATES_READ]
    method_scopes = {
        "PUT": [scopes.TEMPLATES_WRITE],
        "PATCH": [scopes.TEMPLATES_WRITE],
        "DELETE": [scopes.TEMPLATES_WRITE],
    }

    def get(self, request, template_id):
        return self.ok(serialize_template(resolve_template(request.auth, template_id)))

    def put(self, request, template_id):
        template = resolve_template(request.auth, template_id)
        serializer = TemplateWriteSerializer(template, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():  # see the note in `post`
                template = serializer.save()
        except IntegrityError as exc:
            raise PublicAPIError(
                INVALID_REQUEST, "Another template in this workspace already uses that name.", status_code=409,
            ) from exc
        return self.ok(serialize_template(template))

    def patch(self, request, template_id):
        return self.put(request, template_id)

    def delete(self, request, template_id):
        template = resolve_template(request.auth, template_id)
        # A template a campaign points at is not deleted, because
        # Campaign.template is SET_NULL and the campaign would silently lose
        # the content it reports having sent. Deactivating is what the caller
        # almost always means, and it is reversible.
        if template.campaigns.exists():
            raise PublicAPIError(
                INVALID_TEMPLATE,
                "This template is used by one or more campaigns. Set is_active to false instead.",
                status_code=409,
            )
        template.delete()
        return self.ok({"deleted": True}, status_code=200)
