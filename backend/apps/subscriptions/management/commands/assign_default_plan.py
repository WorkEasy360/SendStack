"""
Put every customer tenant that has no subscription onto a named plan.

The safe half of "existing data has no subscriptions". `BILLING_DEFAULT_PLAN`
alone changes what those tenants are *allowed* without recording anything, and
a deployment that wants the plan to be visible -- on the customer's own usage
screen, in the reseller's list, in a report -- needs real rows. This writes
them, and nothing else: it never touches a tenant that already has a
subscription, never creates a plan, and never picks one by guessing.

    python manage.py assign_default_plan --plan Starter --dry-run
    python manage.py assign_default_plan --plan Starter
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.subscriptions.models import Plan, Subscription
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Subscribe customer tenants that have no subscription to a default plan."

    def add_arguments(self, parser):
        parser.add_argument(
            "--plan",
            help="Plan name. Defaults to settings.BILLING_DEFAULT_PLAN.",
        )
        parser.add_argument(
            "--status",
            default=Subscription.Status.ACTIVE,
            choices=[value for value, _ in Subscription.Status.choices],
            help="Status to create the subscriptions with (default: ACTIVE).",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what would change and write nothing.",
        )

    def handle(self, *args, **options):
        from django.conf import settings

        name = (options.get("plan") or getattr(settings, "BILLING_DEFAULT_PLAN", "") or "").strip()
        if not name:
            raise CommandError(
                "Name a plan with --plan, or set BILLING_DEFAULT_PLAN. This command never picks one for you."
            )

        plan = Plan.objects.filter(owner__isnull=True, name=name).first()
        if plan is None:
            raise CommandError(
                f'No platform-owned plan called "{name}". Create it first -- '
                "a plan a reseller owns is theirs to assign, not this command's."
            )

        # Customers only. A reseller or the platform itself running the shop
        # is not a customer of it, and putting them on a customer plan would
        # cap the accounts that administer everyone else's.
        missing = Tenant.objects.filter(
            kind=Tenant.Kind.CUSTOMER, subscription__isnull=True,
        ).order_by("pk")

        total = missing.count()
        if not total:
            self.stdout.write("Every customer tenant already has a subscription. Nothing to do.")
            return

        if options["dry_run"]:
            for tenant in missing[:50]:
                self.stdout.write(f"  would subscribe #{tenant.pk} {tenant.name} -> {plan.name}")
            if total > 50:
                self.stdout.write(f"  ... and {total - 50} more")
            self.stdout.write(self.style.WARNING(f"Dry run: {total} tenant(s) would be subscribed to {plan.name}."))
            return

        created = 0
        with transaction.atomic():
            for tenant in missing:
                # get_or_create rather than create: two operators running this
                # at once must not produce a duplicate, and the OneToOne would
                # only tell them so with an IntegrityError halfway through.
                _, was_created = Subscription.objects.get_or_create(
                    tenant=tenant, defaults={"plan": plan, "status": options["status"]},
                )
                created += int(was_created)

        self.stdout.write(self.style.SUCCESS(f"Subscribed {created} tenant(s) to {plan.name}."))
