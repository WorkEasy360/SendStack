"""
Write a starting set of public, platform-owned plans.

A commercialization phase needs something on the pricing page, and a
deployment cannot self-service anybody onto a plan that does not exist. This
writes four -- a trial and three tiers -- and stops.

**It is a starting point, not a commercial decision.** The prices here are
placeholders in the deployment's configured currency and the operator is
expected to edit them; the command says so, and `--dry-run` shows what it
would do first.

    python manage.py seed_plans --dry-run
    python manage.py seed_plans
    python manage.py seed_plans --currency USD --publish

Safe to run more than once. It never overwrites a plan that already exists --
a second run reports what is already there and changes nothing, because the
one thing this must never do is reset a price somebody has customers on.
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.subscriptions.models import Plan

#: The starting catalogue. Amounts are in the currency's **minor unit** --
#: paise for INR, cents for USD -- because that is what `Plan.price_cents`
#: holds and what every payment provider expects.
#:
#: Limits use this codebase's convention throughout: **0 means unlimited**.
SEED_PLANS = (
    {
        "name": "Trial",
        "description": "A free trial with room to connect a number, import contacts and run a campaign.",
        "price_cents": 0,
        "interval": Plan.Interval.MONTHLY,
        "max_messages_per_month": 500,
        "max_contacts": 500,
        "max_devices": 1,
        "max_users": 2,
        "features": {"ai_agent": True, "workflows": False, "api_access": False, "click_tracking": True, "number_filter": True, "group_grabber": True},
        "display_order": 0,
        # Not on the pricing page: a trial is something a signup starts, not
        # a product somebody buys. Listing it as a plan would invite "why
        # would I pay when there is a free tier".
        "is_public": False,
    },
    {
        "name": "Starter",
        "description": "For a small business sending to its own customer list.",
        "price_cents": 99900,
        "interval": Plan.Interval.MONTHLY,
        "max_messages_per_month": 5000,
        "max_contacts": 5000,
        "max_devices": 1,
        "max_users": 3,
        "features": {"ai_agent": False, "workflows": False, "api_access": False, "click_tracking": True, "number_filter": True, "group_grabber": True},
        "display_order": 1,
        "is_public": True,
    },
    {
        "name": "Growth",
        "description": "Automation, an AI agent and more numbers, for a team that messages every day.",
        "price_cents": 299900,
        "interval": Plan.Interval.MONTHLY,
        "max_messages_per_month": 25000,
        "max_contacts": 25000,
        "max_devices": 3,
        "max_users": 10,
        "features": {"ai_agent": True, "workflows": True, "api_access": False, "click_tracking": True, "number_filter": True, "group_grabber": True},
        "display_order": 2,
        "is_public": True,
    },
    {
        "name": "Enterprise",
        "description": "Unlimited volume and API access.",
        "price_cents": 999900,
        "interval": Plan.Interval.MONTHLY,
        # 0 is unlimited, the convention this codebase uses everywhere.
        "max_messages_per_month": 0,
        "max_contacts": 0,
        "max_devices": 0,
        "max_users": 0,
        "features": {"ai_agent": True, "workflows": True, "api_access": True, "click_tracking": True, "number_filter": True, "group_grabber": True},
        "display_order": 3,
        "is_public": True,
    },
)


class Command(BaseCommand):
    help = "Create a starting set of platform-owned plans. Never overwrites an existing plan."

    def add_arguments(self, parser):
        parser.add_argument(
            "--currency",
            help="ISO 4217 code for the seeded plans. Defaults to settings.BILLING_CURRENCY.",
        )
        parser.add_argument(
            "--publish",
            action="store_true",
            help="Mark the paid tiers public so they appear on the pricing page.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Report and write nothing.")

    def handle(self, *args, **options):
        currency = (options.get("currency") or getattr(settings, "BILLING_CURRENCY", "INR")).upper()
        dry_run = options["dry_run"]
        publish = options["publish"]

        created = skipped = 0
        with transaction.atomic():
            for spec in SEED_PLANS:
                spec = dict(spec)
                name = spec.pop("name")
                # Platform-owned: `owner=None`. A seeded plan belongs to
                # whoever runs the deployment, never to a reseller.
                if Plan.objects.filter(owner__isnull=True, name=name).exists():
                    self.stdout.write(f"  = {name} already exists; left alone.")
                    skipped += 1
                    continue

                if not publish:
                    # Written but unlisted unless asked. An operator who has
                    # not reviewed these prices should not discover them live
                    # on their pricing page.
                    spec["is_public"] = False

                if dry_run:
                    self.stdout.write(
                        f"  + would create {name} at {spec['price_cents']} {currency} "
                        f"({'public' if spec['is_public'] else 'unlisted'})"
                    )
                else:
                    Plan.objects.create(owner=None, name=name, currency=currency, **spec)
                    self.stdout.write(self.style.SUCCESS(f"  + created {name}"))
                created += 1

            if dry_run:
                transaction.set_rollback(True)

        self.stdout.write("")
        verb = "would create" if dry_run else "created"
        self.stdout.write(f"{verb} {created} plan(s), skipped {skipped} that already existed.")
        if created and not dry_run:
            self.stdout.write(self.style.WARNING(
                "\nThese prices are placeholders. Review them in the admin or on the Plans page "
                "before opening signups, and set BILLING_TRIAL_PLAN=Trial to make trials work."
            ))
            if not publish:
                self.stdout.write(
                    "Seeded plans are unlisted. Re-run with --publish, or tick 'is public' on the "
                    "ones you want on the pricing page."
                )
