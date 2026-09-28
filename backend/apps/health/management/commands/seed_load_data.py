"""
Build a realistically-shaped dataset so performance can be measured instead of guessed.

    python manage.py seed_load_data --tenants 100 --contacts 10000 --messages 100000

**This command refuses to run against anything that looks like production.**
Seeding writes tens of thousands of rows carrying fake phone numbers into
every hot table in the schema; doing that to a live workspace would corrupt a
customer's contact list, their consent evidence and their usage counters in
one go. Three independent gates have to agree before a single row is written:

1. ``APP_ENVIRONMENT`` must not be ``production``.
2. The database name must be one of the recognised scratch names, or
   ``--force-database`` must name it explicitly.
3. ``--yes`` must be passed.

The shape matters as much as the size. Real load is not one tenant with a
million contacts -- it is a long tail of small workspaces and a handful of
large ones, all sharing the same tables and the same indexes, which is what
makes ``WHERE tenant_id = %s`` selective or useless. So contacts and messages
are dealt across tenants on a Pareto-ish curve: the first tenant gets a large
share, and the tail gets a few hundred rows each.

Everything is written with ``bulk_create``. Seeding is not the thing being
measured, and a per-row ``save()`` on 100,000 messages would take longer than
every benchmark run put together.
"""

import random
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.campaigns.models import Campaign
from apps.contacts.models import Contact, ContactGroup, ContactTag
from apps.messaging.models import Message
from apps.subscriptions.models import Plan, Subscription
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount

#: Database names this command is willing to write to without --force-database.
#: Deliberately a short allowlist rather than a "does it contain prod" check:
#: a deny-list gets the one name nobody thought of wrong, and that one name is
#: somebody's live customer data.
SCRATCH_DATABASE_NAMES = {"ms_loadtest", "ms_ci", "scratch_ms", "test_multisender", "loadtest"}

#: How the recipients of a batch of messages are distributed over statuses.
#: Roughly what a healthy deployment looks like -- most delivered, a few read,
#: a small failure tail -- so that reports and dashboards aggregate over a
#: realistic mix rather than one status.
STATUS_MIX = (
    (Message.Status.DELIVERED, 55),
    (Message.Status.READ, 25),
    (Message.Status.SENT, 10),
    (Message.Status.FAILED, 5),
    (Message.Status.QUEUED, 3),
    (Message.Status.SKIPPED, 2),
)

_BATCH = 2000


def _weighted_statuses(count, rng):
    values = [status for status, _ in STATUS_MIX]
    weights = [weight for _, weight in STATUS_MIX]
    return rng.choices(values, weights=weights, k=count)


def _share(index, total, amount):
    """
    How much of `amount` the tenant at `index` gets.

    A decaying curve rather than an even split: the first workspace holds
    roughly a third of the data and the tail holds a few rows each, which is
    the distribution that actually stresses a tenant-scoped index.
    """
    weight = 1.0 / (index + 1)
    total_weight = sum(1.0 / (i + 1) for i in range(total))
    return max(1, int(amount * weight / total_weight))


class Command(BaseCommand):
    help = "Seed a disposable database with load-test data. Refuses to touch production."

    def add_arguments(self, parser):
        parser.add_argument("--tenants", type=int, default=100)
        parser.add_argument("--contacts", type=int, default=10000)
        parser.add_argument("--messages", type=int, default=100000)
        parser.add_argument("--campaigns-per-tenant", type=int, default=3)
        parser.add_argument("--seed", type=int, default=1234, help="RNG seed, so runs are comparable.")
        parser.add_argument("--yes", action="store_true", help="Required. Confirms this is a disposable database.")
        parser.add_argument(
            "--force-database",
            default="",
            help="Name the database explicitly to seed one not on the scratch allowlist.",
        )

    def handle(self, *args, **options):
        self._guard(options)

        rng = random.Random(options["seed"])
        started = timezone.now()

        plan = self._plan()
        tenants = self._tenants(options["tenants"], plan)
        accounts = self._accounts(tenants)
        contacts = self._contacts(tenants, options["contacts"], rng)
        campaigns = self._campaigns(tenants, accounts, options["campaigns_per_tenant"])
        self._messages(tenants, accounts, contacts, campaigns, options["messages"], rng)

        elapsed = (timezone.now() - started).total_seconds()
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Seeded in {elapsed:.1f}s."))
        self._report()

    # -- safety ----------------------------------------------------------

    def _guard(self, options):
        if not options["yes"]:
            raise CommandError("Refusing to seed without --yes. This writes fake data into every hot table.")

        environment = (getattr(settings, "APP_ENVIRONMENT", "") or "").strip().lower()
        if environment == "production":
            raise CommandError(
                "APP_ENVIRONMENT is 'production'. This command never seeds a production deployment."
            )

        name = connection.settings_dict.get("NAME") or ""
        allowed = options["force_database"]
        if allowed:
            if name != allowed:
                raise CommandError(
                    f"--force-database was given a different name than the connected database. Refusing."
                )
        elif name not in SCRATCH_DATABASE_NAMES:
            raise CommandError(
                f"Database '{name}' is not on the scratch allowlist "
                f"({', '.join(sorted(SCRATCH_DATABASE_NAMES))}). "
                "Pass --force-database=<name> if it really is disposable."
            )

        self.stdout.write(self.style.WARNING(f"Seeding database '{name}'."))

    # -- fixtures --------------------------------------------------------

    def _plan(self):
        plan, _ = Plan.objects.get_or_create(
            name="Load Test",
            owner=None,
            defaults={
                "description": "Seeded by seed_load_data. Not a commercial plan.",
                # 0 is unlimited throughout this codebase; a seeded workspace
                # must not trip the send gate's ceilings while being measured.
                "max_messages_per_month": 0,
                "max_contacts": 0,
                "max_devices": 0,
                "max_users": 0,
                "is_active": True,
                "is_public": False,
            },
        )
        return plan

    def _tenants(self, count, plan):
        platform, _ = Tenant.objects.get_or_create(
            slug="loadtest-platform",
            defaults={"name": "Load Test Platform", "kind": Tenant.Kind.PLATFORM},
        )

        existing = {t.slug: t for t in Tenant.objects.filter(slug__startswith="loadtest-tenant-")}
        wanted = [f"loadtest-tenant-{i:05d}" for i in range(count)]
        missing = [slug for slug in wanted if slug not in existing]

        Tenant.objects.bulk_create(
            [
                Tenant(
                    name=f"Load Test Workspace {slug.rsplit('-', 1)[1]}",
                    slug=slug,
                    kind=Tenant.Kind.CUSTOMER,
                    parent=platform,
                )
                for slug in missing
            ],
            batch_size=_BATCH,
            ignore_conflicts=True,
        )
        tenants = list(Tenant.objects.filter(slug__in=wanted).order_by("slug"))

        User = get_user_model()
        have_users = set(
            User.objects.filter(tenant__in=tenants).values_list("tenant_id", flat=True)
        )
        # One shared hash: Argon2 costs ~50ms per call by design, so hashing a
        # thousand seeded passwords individually would take a minute for no
        # benefit. The password is the same for every seeded account and these
        # accounts only ever exist in a scratch database.
        password_hash = self._shared_password_hash()
        User.objects.bulk_create(
            [
                User(
                    username=f"{tenant.slug}-owner",
                    email=f"{tenant.slug}@example.invalid",
                    password=password_hash,
                    tenant=tenant,
                    role=User.Role.USER,
                    credits=1_000_000,
                    is_active=True,
                )
                for tenant in tenants
                if tenant.pk not in have_users
            ],
            batch_size=_BATCH,
            ignore_conflicts=True,
        )

        have_subscriptions = set(
            Subscription.objects.filter(tenant__in=tenants).values_list("tenant_id", flat=True)
        )
        Subscription.objects.bulk_create(
            [
                Subscription(
                    tenant=tenant,
                    plan=plan,
                    status=Subscription.Status.ACTIVE,
                    current_period_start=timezone.now() - timedelta(days=5),
                    current_period_end=timezone.now() + timedelta(days=25),
                )
                for tenant in tenants
                if tenant.pk not in have_subscriptions
            ],
            batch_size=_BATCH,
            ignore_conflicts=True,
        )

        self.stdout.write(f"  tenants:   {len(tenants)}")
        return tenants

    @staticmethod
    def _shared_password_hash():
        from django.contrib.auth.hashers import make_password

        return make_password("loadtest-password")

    def _accounts(self, tenants):
        User = get_user_model()
        owners = {u.tenant_id: u for u in User.objects.filter(tenant__in=tenants)}
        have = set(WhatsAppAccount.objects.filter(tenant__in=tenants).values_list("tenant_id", flat=True))
        WhatsAppAccount.objects.bulk_create(
            [
                WhatsAppAccount(
                    tenant=tenant,
                    user=owners.get(tenant.pk),
                    name="Seeded line",
                    status=WhatsAppAccount.Status.CONNECTED,
                    phone_number=f"+1500{tenant.pk:07d}",
                )
                for tenant in tenants
                if tenant.pk not in have and owners.get(tenant.pk)
            ],
            batch_size=_BATCH,
            ignore_conflicts=True,
        )
        accounts = {a.tenant_id: a for a in WhatsAppAccount.objects.filter(tenant__in=tenants)}
        self.stdout.write(f"  devices:   {len(accounts)}")
        return accounts

    def _contacts(self, tenants, total, rng):
        User = get_user_model()
        owners = {u.tenant_id: u for u in User.objects.filter(tenant__in=tenants)}
        existing = Contact.objects.filter(tenant__in=tenants).count()
        if existing >= total:
            self.stdout.write(f"  contacts:  {existing} (already seeded)")
            return {
                tenant.pk: list(
                    Contact.objects.filter(tenant=tenant).values_list("pk", flat=True)[:500]
                )
                for tenant in tenants
            }

        first_names = ["Priya", "Rahul", "Anita", "Sam", "Neha", "Arjun", "Meera", "Vikram", "Zoe", "Omar"]
        last_names = ["Sharma", "Patel", "Khan", "Reyes", "Nair", "Iyer", "Das", "Bose", "Rao", "Singh"]

        made = 0
        by_tenant = {}
        for index, tenant in enumerate(tenants):
            want = _share(index, len(tenants), total)
            rows = []
            for n in range(want):
                serial = made + n
                rows.append(
                    Contact(
                        tenant=tenant,
                        created_by=owners.get(tenant.pk),
                        name=f"{rng.choice(first_names)} {rng.choice(last_names)}",
                        # E.164-shaped and inside a documentation range, so a
                        # seeded row can never be a real person's number.
                        phone=f"+1555{serial:07d}",
                        email=f"seed{serial}@example.invalid",
                        company=f"Company {serial % 500}",
                        country="IN",
                        subscribed=rng.random() > 0.05,
                        marketing_consent=rng.choice(
                            [Contact.MarketingConsent.GRANTED, Contact.MarketingConsent.UNKNOWN]
                        ),
                        lifecycle_status=rng.choice(
                            [Contact.Lifecycle.NEW, Contact.Lifecycle.ENGAGED, Contact.Lifecycle.CUSTOMER]
                        ),
                        lead_score=rng.randint(0, 100),
                    )
                )
            Contact.objects.bulk_create(rows, batch_size=_BATCH, ignore_conflicts=True)
            made += want
            by_tenant[tenant.pk] = list(
                Contact.objects.filter(tenant=tenant).values_list("pk", flat=True)[:500]
            )

            ContactGroup.objects.get_or_create(tenant=tenant, name="Seeded group")
            ContactTag.objects.get_or_create(tenant=tenant, name="seeded")

        self.stdout.write(f"  contacts:  {made}")
        return by_tenant

    def _campaigns(self, tenants, accounts, per_tenant):
        User = get_user_model()
        owners = {u.tenant_id: u for u in User.objects.filter(tenant__in=tenants)}
        have = Campaign.objects.filter(tenant__in=tenants).count()
        if have >= len(tenants) * per_tenant:
            self.stdout.write(f"  campaigns: {have} (already seeded)")
        else:
            rows = []
            for tenant in tenants:
                account = accounts.get(tenant.pk)
                if account is None:
                    continue
                for n in range(per_tenant):
                    rows.append(
                        Campaign(
                            tenant=tenant,
                            created_by=owners.get(tenant.pk),
                            name=f"Seeded campaign {n + 1}",
                            whatsapp_account=account,
                            message_body="Hello {{name}}, this is a seeded campaign message.",
                            status=Campaign.Status.COMPLETED,
                            audience_type=Campaign.AudienceType.ALL,
                        )
                    )
            Campaign.objects.bulk_create(rows, batch_size=_BATCH)
            self.stdout.write(f"  campaigns: {len(rows)}")

        return {
            tenant.pk: list(Campaign.objects.filter(tenant=tenant).values_list("pk", flat=True))
            for tenant in tenants
        }

    def _messages(self, tenants, accounts, contacts, campaigns, total, rng):
        existing = Message.objects.filter(tenant__in=tenants).count()
        if existing >= total:
            self.stdout.write(f"  messages:  {existing} (already seeded)")
            return

        User = get_user_model()
        owners = {u.tenant_id: u for u in User.objects.filter(tenant__in=tenants)}
        now = timezone.now()
        made = 0

        for index, tenant in enumerate(tenants):
            account = accounts.get(tenant.pk)
            tenant_contacts = contacts.get(tenant.pk) or []
            tenant_campaigns = campaigns.get(tenant.pk) or []
            if account is None or not tenant_contacts:
                continue

            want = _share(index, len(tenants), total)
            statuses = _weighted_statuses(want, rng)
            rows = []
            for n in range(want):
                status = statuses[n]
                queued = now - timedelta(minutes=rng.randint(0, 60 * 24 * 45))
                sent = queued + timedelta(seconds=rng.randint(1, 30))
                rows.append(
                    Message(
                        tenant=tenant,
                        campaign_id=rng.choice(tenant_campaigns) if tenant_campaigns else None,
                        whatsapp_account=account,
                        contact_id=rng.choice(tenant_contacts),
                        billed_to=owners.get(tenant.pk),
                        to_phone=f"+1555{rng.randint(0, 9999999):07d}",
                        body="Seeded message body.",
                        status=status,
                        queued_at=queued,
                        sent_at=sent if status != Message.Status.QUEUED else None,
                        delivered_at=(
                            sent + timedelta(seconds=5)
                            if status in (Message.Status.DELIVERED, Message.Status.READ)
                            else None
                        ),
                        read_at=(
                            sent + timedelta(seconds=45) if status == Message.Status.READ else None
                        ),
                        failed_at=sent if status == Message.Status.FAILED else None,
                        error_message="Provider rejected the recipient." if status == Message.Status.FAILED else "",
                        plan_counted_at=sent if status != Message.Status.QUEUED else None,
                    )
                )
                if len(rows) >= _BATCH:
                    self._write_messages(rows)
                    made += len(rows)
                    rows = []
            if rows:
                self._write_messages(rows)
                made += len(rows)

        self.stdout.write(f"  messages:  {made}")

    @staticmethod
    def _write_messages(rows):
        # queued_at is auto_now_add, so bulk_create would otherwise stamp
        # every row with "now" and destroy the date spread the report queries
        # are supposed to range over. Writing then correcting keeps the
        # spread without changing the model.
        with transaction.atomic():
            created = Message.objects.bulk_create(rows, batch_size=_BATCH)
            for saved, wanted in zip(created, rows):
                saved.queued_at = wanted.queued_at
            Message.objects.bulk_update(created, ["queued_at"], batch_size=_BATCH)

    def _report(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT relname, n_live_tup
                FROM pg_stat_user_tables
                WHERE n_live_tup > 0
                ORDER BY n_live_tup DESC
                LIMIT 12
                """
            )
            for relname, count in cursor.fetchall():
                self.stdout.write(f"    {relname:<34} {count:>10,}")
