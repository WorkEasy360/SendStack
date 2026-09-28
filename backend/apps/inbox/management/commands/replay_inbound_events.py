"""
Turn inbound webhook events that were stored but never ingested into real
conversations, messages and contacts.

Every inbound message is written down as a ``WhatsAppWebhookEvent`` the moment
it arrives and only afterwards turned into a Conversation + Message by
``apps.inbox.receivers``. When that second step does not happen -- the event
type was written in a casing the receiver did not match, a receiver raised, the
process died between the two -- the event sits in the table with
``processed_at`` empty and the message simply never appears. Nothing retries
it, so those rows are the one place a lost message is still recoverable from.

Replaying deliberately does **not** re-run the automation ladder or the public
API's outbound webhooks. A message that arrived a week ago has long since
stopped being a conversation anybody is waiting on, and answering it now would
send a stranger a reply to something they have forgotten writing. The CRM,
lead-gen, SLA and realtime handlers *do* run: those record what happened rather
than act on it, and they are the reason an unknown number reaches the CRM at
all. ``--notify`` opts back into the full ladder for a message recent enough to
still deserve an answer.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.inbox.services import record_inbound_message
from apps.inbox.signals import inbound_message_recorded
from apps.whatsapp.models import WhatsAppWebhookEvent


def _suppressed_receivers():
    """
    The handlers that must not fire for a message being recovered late.

    Imported here rather than at module scope so this command does not change
    app-loading order, and looked up by function so a disconnect cannot
    silently match nothing the way a string dispatch_uid can.
    """
    from apps.automation.receivers import evaluate_automation_rules
    from apps.publicapi.receivers import on_inbound_message

    return [evaluate_automation_rules, on_inbound_message]


class Command(BaseCommand):
    help = "Ingest inbound WhatsApp webhook events that were stored but never turned into messages."

    def add_arguments(self, parser):
        parser.add_argument(
            "--account", type=int, default=None,
            help="Only replay events for this WhatsAppAccount id.",
        )
        parser.add_argument(
            "--since", default=None,
            help="Only replay events received at or after this ISO-8601 timestamp.",
        )
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Stop after this many events, oldest first.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be replayed and change nothing.",
        )
        parser.add_argument(
            "--notify", action="store_true",
            help=(
                "Also run the automation ladder and the public API's outbound webhooks, "
                "as if the message had just arrived. Only for a message recent enough "
                "that an automatic reply is still wanted."
            ),
        )

    def handle(self, *args, **options):
        events = WhatsAppWebhookEvent.objects.filter(
            event_type__iexact=WhatsAppWebhookEvent.EventType.INBOUND_MESSAGE,
            processed_at__isnull=True,
        ).select_related("account", "account__tenant").order_by("received_at")

        if options["account"]:
            events = events.filter(account_id=options["account"])

        if options["since"]:
            since = parse_datetime(options["since"])
            if since is None:
                raise CommandError(f"--since {options['since']!r} is not an ISO-8601 timestamp.")
            if timezone.is_naive(since):
                since = timezone.make_aware(since)
            events = events.filter(received_at__gte=since)

        if options["limit"]:
            events = events[: options["limit"]]

        events = list(events)
        if not events:
            self.stdout.write("No unprocessed inbound events to replay.")
            return

        if options["dry_run"]:
            self._report_plan(events)
            return

        suppressed = [] if options["notify"] else _suppressed_receivers()
        for handler in suppressed:
            inbound_message_recorded.disconnect(handler)

        recorded = skipped = 0
        try:
            for event in events:
                message = record_inbound_message(event)
                event.processed_at = timezone.now()
                event.save(update_fields=["processed_at"])

                if message is None:
                    # record_inbound_message declines an event it cannot place:
                    # an account with no tenant, or a "from" that is not a
                    # phone number (a LID left by an older bridge). Stamped
                    # processed all the same -- a replay that leaves them
                    # behind would report the same rows as recoverable forever.
                    skipped += 1
                    continue

                inbound_message_recorded.send(
                    sender=message.__class__, message=message,
                    is_new_conversation=getattr(message, "is_new_conversation", False),
                )
                recorded += 1
        finally:
            for handler in suppressed:
                inbound_message_recorded.connect(handler)

        self.stdout.write(self.style.SUCCESS(f"Recovered {recorded} message(s) into the inbox."))
        if skipped:
            self.stdout.write(
                f"Skipped {skipped} event(s) that carried no usable sender -- see the module docstring."
            )
        if suppressed:
            self.stdout.write("Automation and outbound webhooks were not run. Pass --notify to run them.")

    def _report_plan(self, events):
        self.stdout.write(f"{len(events)} unprocessed inbound event(s):")
        for event in events:
            sender = (event.payload.get("message") or {}).get("from") or event.payload.get("from") or "?"
            name = event.payload.get("profile_name") or event.payload.get("push_name") or ""
            self.stdout.write(
                f"  #{event.pk} account={event.account_id} {event.received_at:%Y-%m-%d %H:%M} "
                f"from={sender} {name}".rstrip()
            )
        self.stdout.write("Nothing was changed (--dry-run).")
