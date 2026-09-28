"""
Tests for backup monitoring and the Phase 4 production checks.

What is and is not tested here matters. These prove that the **application**
judges backups correctly: that a local-only backup fails the check, that a
stale one fails, that an unencrypted one warns, that the status endpoint
leaks no path. They do not prove that `backup_postgres.sh` uploads to S3 --
that is a shell script talking to a cloud provider, it is exercised by running
it, and docs/DISASTER_RECOVERY.md says so.

The division is deliberate: the script's job is to report honestly, and this
suite makes sure that an honest report of "local only, three days old,
unencrypted" is treated as the failure it is.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.subscriptions.models import Plan
from apps.tenants.models import Tenant

from .checks import (
    FAIL,
    PASS,
    WARN,
    check_backups,
    check_payment_configuration,
    check_restore_verification,
    check_scheduler,
    check_signup_configuration,
)
from .models import BackupRun


def _run(**overrides):
    fields = {
        "kind": BackupRun.Kind.BACKUP,
        "status": BackupRun.Status.SUCCEEDED,
        "finished_at": timezone.now(),
        "artifact": "multisender-20260830T020000Z.dump.gpg",
        "size_bytes": 41231872,
        "encrypted": True,
        "offsite": True,
        "destination": "s3",
    }
    fields.update(overrides)
    return BackupRun.objects.create(**fields)


class BackupCheckTests(TestCase):
    def test_no_backup_ever_is_a_failure(self):
        result = check_backups()
        self.assertEqual(result["status"], FAIL)

    def test_a_recent_encrypted_offhost_backup_passes(self):
        _run()
        self.assertEqual(check_backups()["status"], PASS)

    def test_a_local_only_backup_fails(self):
        """
        The Phase 3 HIGH risk, asserted. A copy on the same disk as the
        database is not a backup, and calling it "configured" is how a
        deployment believes it is protected for a year.
        """
        _run(offsite=False, destination="local")
        result = check_backups()
        self.assertEqual(result["status"], FAIL)
        self.assertIn("never left this host", result["detail"])

    @override_settings()
    def test_a_local_only_backup_can_be_acknowledged(self):
        """An operator copying it off by other means is a real situation."""
        import os

        _run(offsite=False, destination="local")
        os.environ["BACKUP_OFFSITE_ACKNOWLEDGED"] = "1"
        try:
            self.assertNotEqual(check_backups()["status"], FAIL)
        finally:
            os.environ.pop("BACKUP_OFFSITE_ACKNOWLEDGED", None)

    def test_a_stale_backup_fails(self):
        _run(finished_at=timezone.now() - timedelta(days=5))
        result = check_backups()
        self.assertEqual(result["status"], FAIL)
        self.assertIn("hours ago", result["detail"])

    def test_an_unencrypted_backup_warns(self):
        _run(encrypted=False)
        result = check_backups()
        self.assertEqual(result["status"], WARN)
        self.assertIn("NOT encrypted", result["detail"])

    def test_a_failure_after_a_success_warns(self):
        _run(finished_at=timezone.now() - timedelta(hours=6))
        _run(status=BackupRun.Status.FAILED, finished_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(check_backups()["status"], WARN)

    def test_the_check_never_prints_a_path_or_a_destination_url(self):
        _run(destination="s3://secret-bucket/prefix?key=AKIA")
        detail = check_backups()["detail"]
        self.assertNotIn("secret-bucket", detail)
        self.assertNotIn("AKIA", detail)


class RestoreVerificationCheckTests(TestCase):
    def test_no_restore_ever_warns(self):
        """A backup nobody has restored is a file with a promising name."""
        result = check_restore_verification()
        self.assertEqual(result["status"], WARN)
        self.assertIn("not a backup until it restores", result["detail"])

    def test_a_recent_restore_passes(self):
        _run(kind=BackupRun.Kind.RESTORE_TEST)
        self.assertEqual(check_restore_verification()["status"], PASS)

    def test_an_old_restore_warns(self):
        _run(kind=BackupRun.Kind.RESTORE_TEST, finished_at=timezone.now() - timedelta(days=90))
        self.assertEqual(check_restore_verification()["status"], WARN)

    def test_a_backup_is_not_a_restore_test(self):
        _run()
        self.assertEqual(check_restore_verification()["status"], WARN)


class RecordBackupCommandTests(TestCase):
    def test_the_command_writes_a_run(self):
        call_command(
            "record_backup", "--status", "SUCCEEDED", "--artifact", "db-1.dump.gpg",
            "--bytes", "1024", "--destination", "s3", "--offsite", "--encrypted",
        )
        run = BackupRun.objects.get()
        self.assertEqual(run.status, BackupRun.Status.SUCCEEDED)
        self.assertTrue(run.offsite)
        self.assertTrue(run.encrypted)
        self.assertEqual(run.size_bytes, 1024)

    def test_a_path_is_reduced_to_a_basename(self):
        """A path leaks the host's directory layout into a support screenshot."""
        call_command(
            "record_backup", "--status", "SUCCEEDED",
            "--artifact", "/srv/backups/prod/multisender-2026.dump.gpg",
        )
        self.assertEqual(BackupRun.objects.get().artifact, "multisender-2026.dump.gpg")

    def test_a_windows_path_is_reduced_too(self):
        call_command(
            "record_backup", "--status", "SUCCEEDED",
            "--artifact", r"C:\backups\multisender-2026.dump",
        )
        self.assertEqual(BackupRun.objects.get().artifact, "multisender-2026.dump")

    def test_a_failure_can_be_recorded(self):
        call_command("record_backup", "--status", "FAILED", "--detail", "pg_dump exited 1")
        run = BackupRun.objects.get()
        self.assertEqual(run.status, BackupRun.Status.FAILED)
        self.assertIn("pg_dump", run.detail)


class BackupStatusEndpointTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Ops", slug="ops-tenant", kind=Tenant.Kind.CUSTOMER)
        self.customer = get_user_model().objects.create_user(
            username="ops-customer", password="x", tenant=self.tenant,
        )
        self.admin = get_user_model().objects.create_user(
            username="ops-admin", password="x", tenant=self.tenant,
            role=get_user_model().Role.SUPERADMIN,
        )
        self.url = reverse("health_backups")

    def test_a_customer_cannot_see_it(self):
        client = APIClient()
        client.force_authenticate(self.customer)
        self.assertEqual(client.get(self.url).status_code, 404)

    def test_an_operator_can(self):
        _run()
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.data["last_successful_backup"])
        self.assertIsNotNone(response.data["backup_age_hours"])

    def test_it_reports_no_path_and_no_destination_url(self):
        _run(artifact="multisender-2026.dump.gpg", destination="s3")
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.get(self.url)

        blob = str(response.data)
        self.assertNotIn("/srv/", blob)
        self.assertNotIn("s3://", blob)
        self.assertNotIn("AKIA", blob)

    def test_it_carries_the_verdict_and_not_only_the_numbers(self):
        _run(offsite=False)
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.get(self.url)
        statuses = {row["status"] for row in response.data["assessment"]}
        self.assertIn(FAIL, statuses)


class SchedulerCheckTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Sched Check", slug="sched-check", kind=Tenant.Kind.CUSTOMER,
        )

    def test_an_idle_scheduler_passes(self):
        self.assertEqual(check_scheduler()["status"], PASS)

    def test_overdue_work_fails(self):
        """A stopped beat container means scheduled campaigns are not sending."""
        from apps.scheduling.models import ScheduledJob

        ScheduledJob.objects.create(
            tenant=self.tenant, kind=ScheduledJob.Kind.CAMPAIGN_DISPATCH, object_id=1,
            scheduled_for=timezone.now() - timedelta(hours=2),
        )
        result = check_scheduler()
        self.assertEqual(result["status"], FAIL)
        self.assertIn("not sending", result["detail"])

    def test_a_future_job_is_not_overdue(self):
        from apps.scheduling.models import ScheduledJob

        ScheduledJob.objects.create(
            tenant=self.tenant, kind=ScheduledJob.Kind.CAMPAIGN_DISPATCH, object_id=2,
            scheduled_for=timezone.now() + timedelta(hours=2),
        )
        self.assertEqual(check_scheduler()["status"], PASS)


class SignupConfigurationCheckTests(TestCase):
    @override_settings(SELF_SERVICE_SIGNUP_ENABLED=False)
    def test_signups_off_is_a_valid_configuration(self):
        self.assertEqual(check_signup_configuration()["status"], PASS)

    @override_settings(
        SELF_SERVICE_SIGNUP_ENABLED=True, TRIALS_ENABLED=True, BILLING_TRIAL_PLAN="Nope",
    )
    def test_signups_open_with_no_trial_plan_fails(self):
        """
        The quiet catastrophe: every new customer lands on no subscription,
        which this codebase treats as unlimited.
        """
        result = check_signup_configuration()
        self.assertEqual(result["status"], FAIL)
        self.assertIn("UNLIMITED", result["detail"])

    @override_settings(
        SELF_SERVICE_SIGNUP_ENABLED=True, TRIALS_ENABLED=True,
        BILLING_TRIAL_PLAN="Trial", BILLING_TRIAL_DAYS=14,
    )
    def test_a_configured_deployment_passes(self):
        Plan.objects.create(owner=None, name="Trial", price_cents=0, is_active=True)
        self.assertEqual(check_signup_configuration()["status"], PASS)

    @override_settings(SELF_SERVICE_SIGNUP_ENABLED=True, TRIALS_ENABLED=False)
    def test_signups_open_without_trials_warns(self):
        self.assertEqual(check_signup_configuration()["status"], WARN)


class PaymentConfigurationCheckTests(TestCase):
    @override_settings(PAYMENTS_ENABLED=False)
    def test_payments_off_is_a_valid_configuration(self):
        result = check_payment_configuration()
        self.assertEqual(result["status"], PASS)
        self.assertIn("assigned by an operator", result["detail"])

    @override_settings(
        PAYMENTS_ENABLED=True, PAYMENT_PROVIDER="razorpay",
        RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET="",
    )
    def test_payments_on_without_credentials_fails(self):
        """The worst of the three states: a button that exists and then fails."""
        result = check_payment_configuration()
        self.assertEqual(result["status"], FAIL)
        self.assertIn("no credentials", result["detail"])

    @override_settings(
        PAYMENTS_ENABLED=True, PAYMENT_PROVIDER="razorpay",
        RAZORPAY_KEY_ID="k", RAZORPAY_KEY_SECRET="s", RAZORPAY_WEBHOOK_SECRET="",
    )
    def test_payments_on_without_a_webhook_secret_warns(self):
        self.assertEqual(check_payment_configuration()["status"], WARN)

    @override_settings(
        PAYMENTS_ENABLED=True, PAYMENT_PROVIDER="razorpay",
        RAZORPAY_KEY_ID="k", RAZORPAY_KEY_SECRET="s", RAZORPAY_WEBHOOK_SECRET="wh",
    )
    def test_a_fully_configured_provider_passes(self):
        self.assertEqual(check_payment_configuration()["status"], PASS)

    @override_settings(PAYMENTS_ENABLED=True, PAYMENT_PROVIDER="manual")
    def test_payments_on_with_the_manual_provider_warns(self):
        self.assertEqual(check_payment_configuration()["status"], WARN)

    @override_settings(
        PAYMENTS_ENABLED=True, PAYMENT_PROVIDER="razorpay",
        RAZORPAY_KEY_ID="rzp_live_verysecret", RAZORPAY_KEY_SECRET="topsecret",
        RAZORPAY_WEBHOOK_SECRET="whsecret",
    )
    def test_the_check_never_prints_a_credential(self):
        detail = check_payment_configuration()["detail"]
        self.assertNotIn("topsecret", detail)
        self.assertNotIn("whsecret", detail)
        self.assertNotIn("rzp_live_verysecret", detail)
