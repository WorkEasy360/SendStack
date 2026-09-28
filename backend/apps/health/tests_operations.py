"""
The operations dashboard and the dead-letter record.

Two things are being checked and the second is the one that would hurt if it
regressed: that the panels report what an operator needs, and that they report
*nothing else* -- no message content, no customer data, no broker URL and no
credential. An operations screen is exactly where those get added.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.health import deadletters, operations
from apps.health.models import BackupRun, DeadLetter
from apps.tenants.models import Tenant


class SnapshotTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Ops Co", slug="ops-co", kind=Tenant.Kind.CUSTOMER,
        )

    def test_every_declared_panel_appears(self):
        snapshot = operations.snapshot()

        for name, _ in operations.SECTIONS:
            with self.subTest(panel=name):
                self.assertIn(name, snapshot)

    def test_a_broken_panel_degrades_instead_of_taking_the_page_down(self):
        """
        The behaviour that makes this usable during an incident.

        A dashboard read at 3am with Redis down must render the panels that
        work; one that 500s is useless precisely when it is needed.
        """
        def explode():
            raise RuntimeError("panel is broken")

        with patch.object(operations, "SECTIONS", (("scheduler", explode),)):
            snapshot = operations.snapshot()

        self.assertEqual(snapshot["scheduler"], {"error": "unavailable"})
        self.assertIn("generated_at", snapshot)

    def test_queue_depths_list_every_configured_queue_even_when_the_broker_is_down(self):
        """
        A queue missing from the report is indistinguishable from a queue at
        zero -- and a routed task whose queue nobody consumes is exactly the
        failure this panel exists to surface.
        """
        from django.conf import settings

        with patch("redis.from_url", side_effect=OSError("no broker")):
            result = operations.queue_depths()

        self.assertFalse(result["available"])
        self.assertEqual(set(result["queues"]), set(settings.CELERY_QUEUE_NAMES))

    def test_messaging_panel_breaks_failures_down_by_reason(self):
        from apps.contacts.models import Contact
        from apps.messaging.models import Message
        from apps.whatsapp.models import WhatsAppAccount

        user = get_user_model().objects.create_user(
            username="ops-owner", password="x", tenant=self.tenant,
        )
        account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=user, name="Line",
        )
        contact = Contact.objects.create(
            tenant=self.tenant, created_by=user, phone="+15553330001",
        )
        for _ in range(3):
            Message.objects.create(
                tenant=self.tenant, whatsapp_account=account, contact=contact,
                to_phone=contact.phone, body="hi", billed_to=user,
                status=Message.Status.FAILED,
                failure_reason=Message.FailureReason.DEVICE_DISCONNECTED,
            )

        panel = operations.messaging()

        reasons = {row["reason"]: row["count"] for row in panel["failure_reasons"]}
        self.assertEqual(reasons.get(Message.FailureReason.DEVICE_DISCONNECTED), 3)
        self.assertEqual(panel["failed_recent"], 3)

    def test_no_panel_leaks_a_credential_or_a_broker_url(self):
        """
        The rule the module docstring states, asserted rather than trusted.

        Rendered to a string and searched, so it catches a secret however
        deeply it is nested in a panel somebody adds later.
        """
        from django.conf import settings

        rendered = repr(operations.snapshot())

        for secret in (
            settings.SECRET_KEY,
            settings.CELERY_BROKER_URL,
            settings.REDIS_URL,
            settings.DATABASES["default"].get("PASSWORD") or "!!unset!!",
        ):
            if not secret or secret == "!!unset!!":
                continue
            with self.subTest(secret=secret[:12]):
                self.assertNotIn(secret, rendered)

    def test_backups_panel_reports_age_without_reporting_a_path(self):
        BackupRun.objects.create(
            kind=BackupRun.Kind.BACKUP, status=BackupRun.Status.SUCCEEDED,
            artifact="dump.sql.gz", destination="s3", offsite=True,
        )

        panel = operations.backups()

        self.assertIsNotNone(panel["last_success_at"])
        self.assertTrue(panel["offsite"])
        self.assertNotIn("artifact", panel)


class DeadLetterRecordingTests(TestCase):
    def test_a_terminal_failure_is_recorded(self):
        row = deadletters.record_failure(
            task_name="apps.messaging.tasks.send_message_task",
            task_id="abc-123",
            queue="messages",
            args=(42,),
            exception=RuntimeError("provider refused"),
            attempts=4,
            request_id="req-1",
        )

        self.assertIsNotNone(row)
        self.assertEqual(row.task_name, "apps.messaging.tasks.send_message_task")
        self.assertEqual(row.exception_type, "RuntimeError")
        self.assertEqual(row.attempts, 4)
        self.assertEqual(row.request_id, "req-1")
        self.assertTrue(row.is_open)

    def test_arguments_are_summarised_and_never_stored_raw(self):
        """
        A primary key is useful and safe; a string argument is neither.

        Tasks here take ids today, but an operations table must not become a
        second copy of customer content because a task added later happens to
        take a phone number.
        """
        row = deadletters.record_failure(
            task_name="t",
            args=(7, "+15551234567"),
            kwargs={"token": "super-secret", "body": "Hello Priya", "import_id": 3},
            exception=ValueError("x"),
        )

        self.assertIn("7", row.arguments)
        self.assertNotIn("+15551234567", row.arguments)
        self.assertNotIn("super-secret", row.arguments)
        self.assertNotIn("Hello Priya", row.arguments)
        self.assertIn("<redacted>", row.arguments)
        self.assertIn("import_id=3", row.arguments)

    def test_the_recorder_never_raises(self):
        """
        It runs inside the worker's own failure path.

        An exception here would replace the real failure with this module's
        failure -- turning a diagnosable problem into a confusing one.
        """
        with patch.object(DeadLetter.objects, "create", side_effect=RuntimeError("db down")):
            self.assertIsNone(deadletters.record_failure(task_name="t", exception=ValueError("x")))

    def test_the_signal_handler_swallows_its_own_errors(self):
        with patch.object(deadletters, "record_failure", side_effect=RuntimeError("boom")):
            # No assertion beyond "this returns" -- that is the contract.
            deadletters._on_task_failure(sender=None, task_id="x", exception=ValueError("y"))


class OperationsEndpointTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.tenant = Tenant.objects.create(
            name="Ops Platform", slug="ops-platform", kind=Tenant.Kind.PLATFORM,
        )
        self.admin = User.objects.create_user(
            username="ops-admin", password="x", tenant=self.tenant, role=User.Role.SUPERADMIN,
        )
        self.customer_tenant = Tenant.objects.create(
            name="Ops Customer", slug="ops-customer", kind=Tenant.Kind.CUSTOMER,
        )
        self.customer = User.objects.create_user(
            username="ops-customer-user", password="x", tenant=self.customer_tenant,
        )
        self.client = APIClient()

    def test_superadmin_sees_the_dashboard(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/health/operations/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        for name, _ in operations.SECTIONS:
            self.assertIn(name, payload)

    def test_a_customer_gets_404_not_403(self):
        """
        404 rather than 403, matching the backup endpoint.

        403 confirms the endpoint exists, which is information a customer has
        no use for and an attacker does.
        """
        self.client.force_authenticate(user=self.customer)

        self.assertEqual(self.client.get("/health/operations/").status_code, 404)
        self.assertEqual(self.client.get("/health/dead-letters/").status_code, 404)

    def test_dead_letters_can_be_listed_and_resolved(self):
        row = DeadLetter.objects.create(task_name="t", detail="failed")
        self.client.force_authenticate(user=self.admin)

        listed = self.client.get("/health/dead-letters/")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([r["id"] for r in listed.json()["results"]], [row.pk])

        resolved = self.client.post("/health/dead-letters/", {"ids": [row.pk]}, format="json")
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json()["resolved"], 1)

        row.refresh_from_db()
        self.assertIsNotNone(row.resolved_at)

        # Resolved rows drop out of the default (open-only) listing.
        self.assertEqual(self.client.get("/health/dead-letters/").json()["results"], [])

    def test_resolving_is_audited(self):
        """
        Marking a platform-wide failure record handled is an administrative
        act on the operational record, and Phase 3's rule is that those are
        written down.
        """
        from apps.audit.models import AuditEvent

        row = DeadLetter.objects.create(task_name="t")
        self.client.force_authenticate(user=self.admin)

        self.client.post("/health/dead-letters/", {"ids": [row.pk]}, format="json")

        self.assertTrue(
            AuditEvent.objects.filter(action="operations.dead_letters_resolved").exists()
        )

    def test_resolve_rejects_a_body_that_is_not_a_list_of_ids(self):
        self.client.force_authenticate(user=self.admin)

        for body in ({"ids": "1"}, {"ids": ["a"]}, {"ids": {"1": 1}}):
            with self.subTest(body=body):
                self.assertEqual(
                    self.client.post("/health/dead-letters/", body, format="json").status_code, 400
                )

    def test_liveness_stays_cheap_and_unauthenticated(self):
        """
        Phase 1's rule, re-asserted because this phase added endpoints beside it.

        Liveness must not consult the database, Redis or a throttle -- a probe
        that is slow or rate-limited kills healthy containers during an outage.
        """
        response = APIClient().get("/health/live/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "alive")
