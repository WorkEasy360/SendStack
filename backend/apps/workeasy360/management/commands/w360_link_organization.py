"""
Point a WorkEasy360 organization's sign-ins at an existing SendStack workspace.

    python manage.py w360_link_organization <tenant id> <organization uuid> --actor "<who>"

For a customer who already uses SendStack and now arrives through
WorkEasy360: their organization's people land in the workspace they already
have instead of a new, empty one. Run it **before** that organization's first
WorkEasy360 sign-in; afterwards the organization already has a workspace and
the command refuses (a linked organization is never re-pointed).

Refuses the platform's tenant, a reseller's tenant, a suspended workspace,
and a workspace that holds reseller or platform accounts. A workspace that
is a reseller's customer is refused unless ``--detach-from-reseller`` moves
it under the platform tenant first: a reseller must not stand above a
WorkEasy360 organization. Every link is written to the audit log.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import Tenant
from apps.workeasy360 import services


class Command(BaseCommand):
    help = "Link an existing workspace (tenant) to a WorkEasy360 organization."

    def add_arguments(self, parser):
        parser.add_argument("tenant_id", type=int)
        parser.add_argument("organization_id", help="The WorkEasy360 organization's UUID.")
        parser.add_argument("--actor", required=True, help="Who is doing this, for the audit log.")
        parser.add_argument(
            "--detach-from-reseller", action="store_true",
            help="Move a reseller's customer workspace under the platform tenant first.",
        )

    def handle(self, *args, **options):
        tenant = Tenant.objects.filter(pk=options["tenant_id"]).first()
        if tenant is None:
            raise CommandError(f"No workspace with id {options['tenant_id']}.")
        try:
            link = services.link_organization(
                tenant=tenant,
                organization_id=options["organization_id"],
                actor=options["actor"],
                detach_from_reseller=options["detach_from_reseller"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"WorkEasy360 organization {link.organization_id} signs in to workspace "
            f"{link.tenant_id} ({tenant.name})."
        ))
