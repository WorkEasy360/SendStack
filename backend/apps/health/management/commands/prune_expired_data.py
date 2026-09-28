"""
Run the retention rules by hand, or see what they would remove.

    python manage.py prune_expired_data --dry-run
    python manage.py prune_expired_data
    python manage.py prune_expired_data --rule whatsapp_webhook_events

The scheduled sweep (`apps.health.tasks.run_retention_sweep`, daily) is what
normally does this. The command exists for the first run on a deployment that
has been accumulating for a year -- where an operator wants to see the size of
the backlog before letting anything delete it -- and for a host where beat is
not running.

``--dry-run`` counts and deletes nothing. It is the default answer to "what
would this do to my database", and it is worth running first, because the
numbers on an old deployment are usually larger than anybody expects.
"""

from django.core.management.base import BaseCommand

from apps.health import retention


class Command(BaseCommand):
    help = "Delete operational data that has passed its retention horizon. Never touches customer data."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would be removed; delete nothing.")
        parser.add_argument("--rule", default="", help="Run one rule by name instead of all of them.")

    def handle(self, *args, **options):
        names = [name for name, _ in retention.RULES]
        if options["rule"] and options["rule"] not in names:
            self.stderr.write(self.style.ERROR(f"Unknown rule. Choose from: {', '.join(names)}"))
            return

        if options["dry_run"]:
            self._dry_run(options["rule"])
            return

        results = retention.run_all(only=options["rule"] or None)
        self.stdout.write("")
        for name, rows in results.items():
            if rows is None:
                self.stdout.write(f"{self.style.ERROR('FAILED')}  {name}")
            else:
                self.stdout.write(f"{self.style.SUCCESS('ok')}      {name:<28}{rows:>10,} row(s)")

    def _dry_run(self, only):
        """
        Count what each rule matches, without the rule running.

        The counts are worked out here rather than by adding a dry-run flag to
        every rule: a rule whose delete and whose count are two different
        pieces of code is a rule that can report one thing and do another.
        These are deliberately described as *approximate* for that reason --
        the authority on what gets deleted is the rule itself.
        """
        from django.conf import settings
        from django.utils import timezone

        def horizon(setting, default):
            days = int(getattr(settings, setting, default) or 0)
            return None if days <= 0 else timezone.now() - timezone.timedelta(days=days)

        from apps.billing.models import WebhookEvent
        from apps.messaging.models import PendingButtonPrompt
        from apps.scheduling.models import ScheduledJob
        from apps.whatsapp.models import WhatsAppWebhookEvent
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

        checks = []

        cutoff = horizon("RETENTION_WEBHOOK_EVENT_DAYS", 30)
        checks.append((
            "whatsapp_webhook_events",
            0 if cutoff is None else WhatsAppWebhookEvent.objects.filter(received_at__lt=cutoff).count(),
            WhatsAppWebhookEvent.objects.count(),
        ))

        cutoff = horizon("BILLING_WEBHOOK_PAYLOAD_RETENTION_DAYS", 90)
        checks.append((
            "billing_webhook_payloads",
            0 if cutoff is None else WebhookEvent.objects.filter(
                received_at__lt=cutoff, payload_purged_at__isnull=True
            ).count(),
            WebhookEvent.objects.count(),
        ))

        cutoff = horizon("RETENTION_SCHEDULED_JOB_DAYS", 30)
        checks.append((
            "scheduled_jobs",
            0 if cutoff is None else ScheduledJob.objects.filter(
                status__in=[ScheduledJob.Status.COMPLETED, ScheduledJob.Status.CANCELLED],
                updated_at__lt=cutoff,
            ).count(),
            ScheduledJob.objects.count(),
        ))

        cutoff = horizon("RETENTION_BUTTON_PROMPT_DAYS", 7)
        checks.append((
            "button_prompts",
            0 if cutoff is None else PendingButtonPrompt.objects.filter(expires_at__lt=cutoff).count(),
            PendingButtonPrompt.objects.count(),
        ))

        checks.append((
            "expired_refresh_tokens",
            OutstandingToken.objects.filter(expires_at__lt=timezone.now()).count(),
            OutstandingToken.objects.count(),
        ))

        self.stdout.write("")
        self.stdout.write(f"{'rule':<28}{'would remove':>15}{'table total':>15}")
        self.stdout.write("-" * 58)
        for name, matched, total in checks:
            if only and name != only:
                continue
            self.stdout.write(f"{name:<28}{matched:>15,}{total:>15,}")
        self.stdout.write("")
        self.stdout.write("Approximate. Nothing was deleted.")
