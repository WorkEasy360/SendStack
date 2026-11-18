"""
What WorkEasy360 sign-in looks like on this deployment, read-only.

    python manage.py w360_organizations

One line per linked organization (its workspace, how it was linked, its
people and their live WorkEasy360 sessions), then the people per issuer --
which tells you whether an issuer move (``w360sso_move_issuer``) is needed.
"""

from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from apps.workeasy360 import services
from apps.workeasy360.models import OrganizationLink


class Command(BaseCommand):
    help = "List linked WorkEasy360 organizations, their workspaces and live sessions."

    def handle(self, *args, **options):
        from workeasy360_sso.models import FederatedIdentity, ProductSession

        cfg = services.config()
        self.stdout.write(f"configured: {services.enabled()}  sso_only: {services.sso_only()}  "
                          f"issuer: {cfg.issuer if cfg is not None else ''}")
        links = OrganizationLink.objects.select_related("tenant").order_by("created_at")
        for link in links:
            people = FederatedIdentity.objects.filter(user__tenant_id=link.tenant_id).count()
            live = ProductSession.objects.filter(organization_id=link.organization_id, ended_at__isnull=True).count()
            self.stdout.write(
                f"{link.organization_id}  workspace {link.tenant_id} ({link.tenant.name})"
                f"{'' if link.tenant.is_active else ' SUSPENDED'}  {link.origin} by {link.linked_by or '-'}  "
                f"people {people}  live sessions {live}"
            )
        if not links:
            self.stdout.write("No organization is linked yet.")
        by_issuer = FederatedIdentity.objects.values("issuer").annotate(
            people=Count("id"),
            live=Count("sessions", filter=Q(sessions__ended_at__isnull=True)),
        ).order_by("issuer")
        for row in by_issuer:
            self.stdout.write(f"issuer {row['issuer']}: {row['people']} people, {row['live']} live sessions")
