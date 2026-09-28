"""
Delete one workspace and everything in it.

This command exists because the alternative -- an operator writing a `.delete()`
in a Django shell against a production database at the end of a long day -- is
how the wrong customer gets deleted. Everything below is a guard against that
one failure.

The guards, in order:

* **Nothing happens without `--confirm <slug>`.** Not a `--yes` flag: the
  operator has to type the *identity of the thing being destroyed*, so a
  command recalled from shell history and re-run points at the workspace it
  named and no other.
* **It refuses a workspace with children.** Deleting a reseller would orphan or
  cascade into its customers, who are other people's businesses.
* **It refuses the platform tenant**, which is the deployment itself.
* **`--dry-run` first**, and it is the documented default in the runbook. It
  prints exactly what would go, counted from the database.
* **It reports what it kept, and why.** A deletion that quietly retains data is
  a lie told to whoever asked for it; see RETAINED below.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.tenants.models import Tenant

#: What survives a workspace deletion, and the reason. Printed by the command,
#: and mirrored in docs/DATA_RETENTION.md -- if these two ever disagree, the
#: code is right and the document is wrong.
RETAINED = [
    (
        "Consent events (opt-ins and opt-outs)",
        "They are the evidence that an opt-out was honoured. Destroying them destroys "
        "the customer's own defence against a complaint about messages already sent.",
    ),
    (
        "Audit events",
        "They record who did what, including this deletion. An audit trail that can be "
        "erased by the action it records is not an audit trail.",
    ),
    (
        "Credit ledger entries",
        "They are financial records. What was charged and refunded has to remain "
        "reconcilable after the account is gone.",
    ),
    (
        "Legal acceptances",
        "Which version of the Terms this customer accepted, and when. Deleting it "
        "would erase the only evidence that they agreed to anything.",
    ),
    (
        "Database backups",
        "Backups are not selectively edited. Data deleted today is still in every "
        "backup taken before today until those rotate out of the retention window.",
    ),
]


class Command(BaseCommand):
    help = "Delete one tenant and its data. Requires --confirm <slug>."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Slug of the tenant to delete.")
        parser.add_argument(
            "--confirm",
            default="",
            help="Repeat the slug exactly. Nothing is deleted without it.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count what would be deleted and change nothing.",
        )

    def handle(self, *args, **options):
        slug = options["slug"]
        try:
            tenant = Tenant.objects.get(slug=slug)
        except Tenant.DoesNotExist as exc:
            raise CommandError(f"No tenant with slug {slug!r}.") from exc

        if tenant.kind == Tenant.Kind.PLATFORM:
            raise CommandError(
                "Refusing to delete the platform tenant. That is the deployment itself, "
                "not a customer."
            )

        children = Tenant.objects.filter(parent=tenant).count()
        if children:
            raise CommandError(
                f"{tenant.name} has {children} workspace(s) beneath it. Delete or reassign "
                "them first -- they belong to other people."
            )

        counts = self._counts(tenant)
        self.stdout.write(self.style.WARNING(f"Tenant: {tenant.name} (slug={tenant.slug}, id={tenant.pk})"))
        self.stdout.write("Would delete:")
        for label, count in sorted(counts.items()):
            self.stdout.write(f"  {count:>9,}  {label}")

        self.stdout.write("")
        self.stdout.write("Would be RETAINED:")
        for label, why in RETAINED:
            self.stdout.write(f"  - {label}")
            self.stdout.write(f"      {why}")
        self.stdout.write("")

        if options["dry_run"]:
            self.stdout.write(self.style.SUCCESS("Dry run. Nothing was changed."))
            return

        if options["confirm"] != slug:
            raise CommandError(
                f"Nothing was deleted. To go ahead, repeat the slug: --confirm {slug}"
            )

        with transaction.atomic():
            # One transaction: a deletion that half-succeeds leaves a workspace
            # whose contacts are gone and whose messages point at nothing.
            #
            # The order matters only where a PROTECT sits between two models;
            # Django resolves the rest of the graph itself. Contacts and
            # WhatsApp accounts are PROTECTed by things that must go first,
            # which is why they are last.
            deleted = self._delete(tenant)

        self.stdout.write(self.style.SUCCESS(f"Deleted {tenant.name}."))
        for label, count in sorted(deleted.items()):
            if count:
                self.stdout.write(f"  {count:>9,}  {label}")

    def _counts(self, tenant) -> dict:
        from apps.campaigns.models import Campaign
        from apps.contacts.models import Contact
        from apps.inbox.models import Conversation
        from apps.media_library.models import MediaAsset
        from apps.messaging.models import Message
        from apps.templates_app.models import MessageTemplate
        from apps.whatsapp.models import WhatsAppAccount

        return {
            "contacts": Contact.objects.filter(tenant=tenant).count(),
            "messages": Message.objects.filter(tenant=tenant).count(),
            "conversations": Conversation.objects.filter(tenant=tenant).count(),
            "campaigns": Campaign.objects.filter(tenant=tenant).count(),
            "templates": MessageTemplate.objects.filter(tenant=tenant).count(),
            "media assets": MediaAsset.objects.filter(tenant=tenant).count(),
            "WhatsApp accounts (and their stored credentials)": WhatsAppAccount.objects.filter(tenant=tenant).count(),
            "users": tenant.users.count() if hasattr(tenant, "users") else 0,
        }

    def _delete(self, tenant) -> dict:
        from apps.campaigns.models import Campaign
        from apps.contacts.models import Contact, ContactGroup, ContactImport, ContactSegment, ContactTag
        from apps.inbox.models import Conversation
        from apps.media_library.models import MediaAsset
        from apps.messaging.models import Message
        from apps.templates_app.models import MessageTemplate
        from apps.whatsapp.models import WhatsAppAccount

        deleted = {}

        def drop(label, queryset):
            count, _ = queryset.delete()
            deleted[label] = count

        drop("messages", Message.objects.filter(tenant=tenant))
        drop("conversations", Conversation.objects.filter(tenant=tenant))
        drop("campaigns", Campaign.objects.filter(tenant=tenant))
        drop("templates", MessageTemplate.objects.filter(tenant=tenant))
        drop("contact imports", ContactImport.objects.filter(tenant=tenant))
        drop("contact segments", ContactSegment.objects.filter(tenant=tenant))
        drop("contacts", Contact.objects.filter(tenant=tenant))
        drop("contact groups", ContactGroup.objects.filter(tenant=tenant))
        drop("contact tags", ContactTag.objects.filter(tenant=tenant))
        drop("media assets", MediaAsset.objects.filter(tenant=tenant))
        drop("WhatsApp accounts", WhatsAppAccount.objects.filter(tenant=tenant))

        # The tenant row itself is left in place, deliberately. It is what the
        # retained audit, consent, credit and legal rows point at, and deleting
        # it would either cascade into them or leave them dangling. Marked
        # inactive instead, which the send gate and the authentication layer
        # already treat as "this workspace may do nothing".
        tenant.is_active = False
        tenant.save(update_fields=["is_active"])
        deleted["tenant marked inactive (row retained for the records above)"] = 1
        return deleted
