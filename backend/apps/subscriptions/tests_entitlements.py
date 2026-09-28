"""
The entitlement matrix: every quota, every feature, every subscription state,
and the ways somebody might try to get round them.

The tests here are deliberately end-to-end where they can be -- through the
API, through the Celery task, through the service the receiver calls -- rather
than against `check_quota` alone. An entitlement that only holds when you call
the entitlement function is not an entitlement; the whole finding this phase
answers was a `check_quota` with almost no call sites.
"""

import threading
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.subscriptions import services
from apps.subscriptions.entitlements import QuotaExceeded
from apps.subscriptions.models import Plan, Subscription
from apps.tenants.models import Tenant

# ---------------------------------------------------------------------------
# The plan feature matrix (spec §33). Test fixtures, not commercial pricing:
# nothing here is written to a customer's own plans.
# ---------------------------------------------------------------------------

PLAN_MATRIX = {
    "Starter": {
        "price_cents": 0,
        "max_messages_per_month": 100,
        "max_contacts": 500,
        "max_devices": 1,
        "max_users": 1,
        "features": {"ai_agent": False, "workflows": False, "api_access": False, "click_tracking": False},
    },
    "Growth": {
        "price_cents": 4900,
        "max_messages_per_month": 10_000,
        "max_contacts": 5_000,
        "max_devices": 3,
        "max_users": 10,
        "features": {"ai_agent": True, "workflows": True, "api_access": False, "click_tracking": True},
    },
    "Enterprise": {
        "price_cents": 29900,
        # 0 is unlimited throughout the product.
        "max_messages_per_month": 0,
        "max_contacts": 0,
        "max_devices": 0,
        "max_users": 0,
        "features": {"ai_agent": True, "workflows": True, "api_access": True, "click_tracking": True},
    },
}


def make_plan(name="Starter", owner=None, **overrides):
    fields = dict(PLAN_MATRIX.get(name, PLAN_MATRIX["Starter"]))
    fields.update(overrides)
    return Plan.objects.create(name=name, owner=owner, **fields)


def customer_on(plan, name="Customer", **subscription_fields):
    tenant = Tenant.objects.create(name=name, kind=Tenant.Kind.CUSTOMER)
    Subscription.objects.create(tenant=tenant, plan=plan, **subscription_fields)
    return tenant


class PlanMatrixTests(TestCase):
    """Each fixture plan grants exactly what the matrix says it does."""

    def test_every_plan_in_the_matrix_grants_what_it_claims(self):
        for name, spec in PLAN_MATRIX.items():
            with self.subTest(plan=name):
                tenant = customer_on(make_plan(name), name=f"Customer on {name}")
                for feature, expected in spec["features"].items():
                    self.assertEqual(
                        services.feature_enabled(tenant.pk, feature), expected,
                        f"{name} should have {feature}={expected}",
                    )

    def test_an_unlimited_plan_never_refuses(self):
        tenant = customer_on(make_plan("Enterprise"))
        for metric in services.METRICS:
            allowed, _ = services.check_quota(tenant.pk, metric, 1_000_000)
            self.assertTrue(allowed, metric)


# ---------------------------------------------------------------------------
# Subscription status (spec §4, §35)
# ---------------------------------------------------------------------------

class SubscriptionStateTests(TestCase):
    def setUp(self):
        self.plan = make_plan("Growth")

    def _subscription(self, **fields):
        tenant = Tenant.objects.create(name=f"T{Tenant.objects.count()}", kind=Tenant.Kind.CUSTOMER)
        return Subscription.objects.create(tenant=tenant, plan=self.plan, **fields)

    def test_active_grants_everything(self):
        subscription = self._subscription(status=Subscription.Status.ACTIVE)
        self.assertEqual(subscription.access_state(), Subscription.FULL)
        self.assertTrue(services.feature_enabled(subscription.tenant_id, "ai_agent"))

    def test_trialing_grants_the_plan_it_is_trialing(self):
        subscription = self._subscription(status=Subscription.Status.TRIALING)
        self.assertEqual(subscription.access_state(), Subscription.FULL)
        allowed, _ = services.check_quota(subscription.tenant_id, "contacts")
        self.assertTrue(allowed)

    def test_past_due_keeps_working_inside_the_grace_window(self):
        """An invoice slipping is not a reason to switch a business off."""
        subscription = self._subscription(
            status=Subscription.Status.PAST_DUE,
            current_period_start=timezone.now() - timezone.timedelta(days=35),
            current_period_end=timezone.now() - timezone.timedelta(days=2),
        )
        self.assertEqual(subscription.access_state(), Subscription.GRACE)
        self.assertTrue(services.feature_enabled(subscription.tenant_id, "workflows"))

    def test_past_due_stops_once_the_grace_window_has_passed(self):
        subscription = self._subscription(
            status=Subscription.Status.PAST_DUE,
            current_period_start=timezone.now() - timezone.timedelta(days=120),
            current_period_end=timezone.now() - timezone.timedelta(days=90),
        )
        self.assertEqual(subscription.access_state(), Subscription.EXPIRED)
        self.assertFalse(services.feature_enabled(subscription.tenant_id, "workflows"))

    @override_settings(BILLING_PAST_DUE_GRACE_DAYS=0)
    def test_the_grace_window_is_configurable(self):
        subscription = self._subscription(
            status=Subscription.Status.PAST_DUE,
            current_period_start=timezone.now() - timezone.timedelta(days=40),
            current_period_end=timezone.now() - timezone.timedelta(hours=1),
        )
        self.assertEqual(subscription.access_state(), Subscription.EXPIRED)

    def test_an_expired_subscription_grants_no_feature_and_no_quota(self):
        subscription = self._subscription(
            status=Subscription.Status.CANCELLED,
            current_period_start=timezone.now() - timezone.timedelta(days=60),
            current_period_end=timezone.now() - timezone.timedelta(days=30),
        )
        for feature in Plan.FEATURES:
            self.assertFalse(services.feature_enabled(subscription.tenant_id, feature), feature)
        for metric in services.METRICS:
            allowed, _ = services.check_quota(subscription.tenant_id, metric)
            self.assertFalse(allowed, metric)

    def test_an_expired_subscription_reports_every_feature_off_to_the_ui(self):
        subscription = self._subscription(
            status=Subscription.Status.CANCELLED,
            current_period_start=timezone.now() - timezone.timedelta(days=60),
            current_period_end=timezone.now() - timezone.timedelta(days=30),
        )
        payload = services.get_entitlements(subscription.tenant_id)
        self.assertEqual(payload["access"], Subscription.EXPIRED)
        self.assertFalse(any(payload["features"].values()))

    def test_a_tenant_with_no_subscription_at_all_is_reported_not_crashed(self):
        tenant = Tenant.objects.create(name="Unsubscribed", kind=Tenant.Kind.CUSTOMER)
        payload = services.get_entitlements(tenant.pk)
        self.assertIsNone(payload["plan"])
        self.assertIsNone(payload["subscription"])
        self.assertTrue(all(payload["features"].values()))


class ExistingDataFallbackTests(TestCase):
    """Spec §24: tenants that predate subscriptions must not break or be promoted."""

    def test_an_operator_tenant_is_never_governed_by_the_default_plan(self):
        make_plan("Starter", owner=None)
        platform = Tenant.objects.create(name="Platform", kind=Tenant.Kind.PLATFORM)
        reseller = Tenant.objects.create(name="Reseller", kind=Tenant.Kind.RESELLER)

        with override_settings(BILLING_DEFAULT_PLAN="Starter"):
            self.assertIsNone(services.get_current_plan(platform.pk))
            self.assertIsNone(services.get_current_plan(reseller.pk))

    def test_a_customer_falls_back_to_the_named_default_plan(self):
        make_plan("Starter", owner=None)
        customer = Tenant.objects.create(name="Legacy", kind=Tenant.Kind.CUSTOMER)

        with override_settings(BILLING_DEFAULT_PLAN="Starter"):
            plan = services.get_current_plan(customer.pk)
            self.assertIsNotNone(plan)
            self.assertEqual(plan.name, "Starter")
            self.assertFalse(services.feature_enabled(customer.pk, "ai_agent"))

    def test_an_unset_default_leaves_existing_tenants_exactly_as_they_were(self):
        make_plan("Enterprise", owner=None)
        customer = Tenant.objects.create(name="Legacy", kind=Tenant.Kind.CUSTOMER)

        self.assertIsNone(services.get_current_plan(customer.pk))
        self.assertTrue(services.feature_enabled(customer.pk, "ai_agent"))

    def test_a_default_naming_no_plan_does_not_guess_one(self):
        """Handing every customer whatever sorted first is the worst failure here."""
        make_plan("Enterprise", owner=None)
        customer = Tenant.objects.create(name="Legacy", kind=Tenant.Kind.CUSTOMER)

        with override_settings(BILLING_DEFAULT_PLAN="Does Not Exist"):
            self.assertIsNone(services.get_current_plan(customer.pk))

    def test_a_resellers_own_plan_is_never_the_platform_default(self):
        reseller_tenant = Tenant.objects.create(name="Reseller", kind=Tenant.Kind.RESELLER)
        make_plan("Starter", owner=reseller_tenant)
        customer = Tenant.objects.create(name="Legacy", kind=Tenant.Kind.CUSTOMER)

        with override_settings(BILLING_DEFAULT_PLAN="Starter"):
            self.assertIsNone(services.get_current_plan(customer.pk))


# ---------------------------------------------------------------------------
# Downgrade (spec §23)
# ---------------------------------------------------------------------------

class DowngradeTests(TestCase):
    def setUp(self):
        from apps.contacts.models import Contact

        self.tenant = customer_on(make_plan("Growth"))
        self.user = get_user_model().objects.create_user(
            username="owner", password="pw", tenant=self.tenant,
        )
        for index in range(6):
            Contact.objects.create(
                tenant=self.tenant, phone=f"+123456780{index:02d}", created_by=self.user,
            )

    def _downgrade_to(self, max_contacts):
        subscription = self.tenant.subscription
        subscription.plan = make_plan("Tiny", max_contacts=max_contacts)
        subscription.save(update_fields=["plan"])

    def test_being_over_the_new_limit_blocks_adding_but_deletes_nothing(self):
        from apps.contacts.models import Contact

        self._downgrade_to(3)

        allowed, reason = services.check_quota(self.tenant.pk, "contacts")

        self.assertFalse(allowed)
        self.assertIn("Tiny", reason)
        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 6)

    def test_the_usage_payload_says_over_limit_rather_than_pretending(self):
        self._downgrade_to(3)

        payload = services.get_entitlements(self.tenant.pk)

        self.assertEqual(payload["usage"]["contacts"]["used"], 6)
        self.assertEqual(payload["usage"]["contacts"]["limit"], 3)
        self.assertEqual(payload["usage"]["contacts"]["remaining"], 0)
        self.assertTrue(payload["usage"]["contacts"]["over_limit"])
        self.assertEqual(payload["usage"]["contacts"]["percentage"], 100)
        self.assertIn("contacts", payload["over_limit"])

    def test_dropping_back_under_the_limit_lets_work_through_again(self):
        from apps.contacts.models import Contact

        self._downgrade_to(3)
        for contact in list(Contact.objects.filter(tenant=self.tenant).order_by("pk")[:4]):
            contact.delete()

        allowed, _ = services.check_quota(self.tenant.pk, "contacts")
        self.assertTrue(allowed)


# ---------------------------------------------------------------------------
# Contacts (spec §7)
# ---------------------------------------------------------------------------

class ContactQuotaTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = customer_on(make_plan("Tiny", max_contacts=2))
        self.user = get_user_model().objects.create_user(
            username="owner", password="pw", tenant=self.tenant,
        )
        self.client.force_authenticate(self.user)

    def _create(self, phone):
        return self.client.post(reverse("contact-list"), {"phone": phone, "name": "X"}, format="json")

    def test_below_the_limit_succeeds(self):
        self.assertEqual(self._create("+12345678001").status_code, 201)

    def test_exactly_at_the_limit_succeeds(self):
        self._create("+12345678001")
        self.assertEqual(self._create("+12345678002").status_code, 201)

    def test_over_the_limit_is_refused_with_a_sentence_a_person_can_act_on(self):
        self._create("+12345678001")
        self._create("+12345678002")

        response = self._create("+12345678003")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "QUOTA_EXCEEDED")
        self.assertEqual(response.data["quota"], "contacts")
        self.assertIn("Tiny", response.data["detail"])
        self.assertIn("Upgrade", response.data["detail"])

    def test_the_refusal_leaks_no_database_detail(self):
        self._create("+12345678001")
        self._create("+12345678002")

        detail = self._create("+12345678003").data["detail"]

        for leak in ("Traceback", "SELECT", "contacts_contact", "QuerySet", "IntegrityError"):
            self.assertNotIn(leak, detail)


class ContactImportQuotaTests(TestCase):
    """Spec §7: the ceiling is enforced against genuinely new contacts."""

    def setUp(self):
        from apps.contacts.models import Contact

        self.client = APIClient()
        self.tenant = customer_on(make_plan("Tiny", max_contacts=3))
        self.user = get_user_model().objects.create_user(
            username="owner", password="pw", tenant=self.tenant,
        )
        self.client.force_authenticate(self.user)
        self.Contact = Contact

    def _upload(self, name, body):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(name, body.encode("utf-8"), content_type="text/csv")

    def _import(self, rows):
        body = "phone,name\n" + "".join(f"{phone},{label}\n" for phone, label in rows)
        return self.client.post(
            reverse("contact-import-list"), {"file": self._upload("c.csv", body)}, format="multipart",
        )

    def test_an_import_that_fits_imports_completely(self):
        response = self._import([("+12345678001", "A"), ("+12345678002", "B")])

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.Contact.objects.filter(tenant=self.tenant).count(), 2)

    def test_an_import_stops_at_the_limit_and_keeps_what_it_already_wrote(self):
        from apps.contacts.models import ContactImport

        response = self._import([
            ("+12345678001", "A"), ("+12345678002", "B"),
            ("+12345678003", "C"), ("+12345678004", "D"), ("+12345678005", "E"),
        ])

        self.assertEqual(response.status_code, 201)
        record = ContactImport.objects.get(pk=response.data["id"])
        self.assertEqual(self.Contact.objects.filter(tenant=self.tenant).count(), 3)
        self.assertEqual(record.created_count, 3)
        # Everything past the row that ran out of room is reported, not lost.
        self.assertGreaterEqual(record.skipped_count, 1)
        self.assertTrue(any("Tiny" in row["error"] for row in record.errors))

    def test_a_reimport_of_existing_contacts_is_not_charged_again(self):
        """A 5,000-row export of a list already on the account adds nothing."""
        self._import([("+12345678001", "A"), ("+12345678002", "B"), ("+12345678003", "C")])

        response = self._import([("+12345678001", "A"), ("+12345678002", "B"), ("+12345678003", "C")])

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.Contact.objects.filter(tenant=self.tenant).count(), 3)

    def test_duplicates_inside_one_file_are_counted_once(self):
        response = self._import([
            ("+12345678001", "A"), ("+12345678001", "A again"), ("+12345678002", "B"),
        ])

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.Contact.objects.filter(tenant=self.tenant).count(), 2)

    def test_the_preview_says_whether_the_file_will_fit(self):
        for index in range(3):
            self.Contact.objects.create(
                tenant=self.tenant, phone=f"+199999900{index:02d}", created_by=self.user,
            )
        body = "phone,name\n+12345678001,A\n+12345678002,B\n"

        response = self.client.post(
            reverse("contact-import-preview"), {"file": self._upload("c.csv", body)}, format="multipart",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["new"], 2)
        self.assertFalse(response.data["within_quota"])
        self.assertIn("Tiny", response.data["quota_warning"])

    def test_an_account_already_full_imports_nothing_new_and_says_so(self):
        """
        Accepted, then stopped at the first row that would add somebody.

        Not refused at upload: a file of contacts already on the account adds
        nothing and must still import, so "the account is full" cannot be a
        reason to reject a file nobody has read yet.
        """
        from apps.contacts.models import ContactImport

        for index in range(3):
            self.Contact.objects.create(
                tenant=self.tenant, phone=f"+199999900{index:02d}", created_by=self.user,
            )

        response = self._import([("+12345678001", "A")])

        self.assertEqual(response.status_code, 201)
        record = ContactImport.objects.get(pk=response.data["id"])
        self.assertEqual(record.created_count, 0)
        self.assertEqual(self.Contact.objects.filter(tenant=self.tenant).count(), 3)
        self.assertTrue(any("Tiny" in row["error"] for row in record.errors))


# ---------------------------------------------------------------------------
# Devices (spec §8)
# ---------------------------------------------------------------------------

class DeviceQuotaTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = customer_on(make_plan("Starter"))  # max_devices = 1
        self.user = get_user_model().objects.create_user(
            username="owner", password="pw", tenant=self.tenant,
        )
        self.client.force_authenticate(self.user)

    def _create(self, name):
        return self.client.post(
            reverse("whatsapp-account-list"),
            {"name": name, "connection_method": "QR", "account_type": "WHATSAPP"},
            format="json",
        )

    def test_the_first_device_is_allowed(self):
        self.assertEqual(self._create("Phone 1").status_code, 201)

    def test_the_second_device_is_refused(self):
        self._create("Phone 1")

        response = self._create("Phone 2")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["quota"], "devices")

    def test_reconnecting_never_consumes_another_slot(self):
        """A device that already holds a slot must not be refused for holding it."""
        from apps.whatsapp.models import WhatsAppAccount

        created = self._create("Phone 1")
        account = WhatsAppAccount.objects.get(pk=created.data["id"])

        with patch(
            "apps.whatsapp.services.WhatsAppConnectionService.start_connection",
            return_value={"success": True, "status": "PENDING", "session_id": "s", "expires_at": None, "message": ""},
        ):
            response = self.client.post(reverse("whatsapp-account-connect", args=[account.pk]))

        self.assertEqual(response.status_code, 200)

    def test_deleting_a_device_frees_its_slot(self):
        from apps.whatsapp.models import WhatsAppAccount

        created = self._create("Phone 1")
        with patch("apps.whatsapp.services.WhatsAppConnectionService.disconnect", return_value={}):
            WhatsAppAccount.objects.get(pk=created.data["id"]).delete()

        self.assertEqual(self._create("Phone 2").status_code, 201)


# ---------------------------------------------------------------------------
# Users (spec §9)
# ---------------------------------------------------------------------------

class UserQuotaTests(TestCase):
    """
    `max_users` counts the active, undeleted users of one tenant.

    Every account this endpoint creates today gets its own workspace, so the
    count is always zero and the ceiling never bites -- which is exactly what
    the audit meant by the team subsystem being unreachable. What is tested
    here is the counting rule and the one path that really does put somebody
    back into an existing workspace, so that the day invites land the ceiling
    is already in place rather than shipped afterwards.
    """

    def setUp(self):
        self.plan = make_plan("Starter")  # max_users = 1
        self.tenant = customer_on(self.plan)
        self.owner = get_user_model().objects.create_user(
            username="owner", password="pw", tenant=self.tenant,
        )

    def test_the_count_is_this_tenants_active_users_and_nobody_elses(self):
        user_model = get_user_model()
        other = Tenant.objects.create(name="Somebody else", kind=Tenant.Kind.CUSTOMER)
        user_model.objects.create_user(username="stranger", password="pw", tenant=other)
        user_model.objects.create_user(
            username="root", password="pw", tenant=other, role=user_model.Role.SUPERADMIN,
        )
        user_model.objects.create_user(
            username="suspended", password="pw", tenant=self.tenant, is_active=False,
        )

        self.assertEqual(services.usage(self.tenant.pk)["users"], 1)

    def test_at_the_limit_the_next_seat_is_refused(self):
        allowed, reason = services.check_quota(self.tenant.pk, "max_users")

        self.assertFalse(allowed)
        self.assertIn("team members", reason)

    def test_reactivating_a_user_into_a_full_workspace_is_refused(self):
        user_model = get_user_model()
        superadmin_tenant = Tenant.objects.create(name="Platform", kind=Tenant.Kind.PLATFORM)
        superadmin = user_model.objects.create_user(
            username="root", password="pw", tenant=superadmin_tenant, role=user_model.Role.SUPERADMIN,
        )
        suspended = user_model.objects.create_user(
            username="second", password="pw", tenant=self.tenant, is_active=False,
        )

        client = APIClient()
        client.force_authenticate(superadmin)
        response = client.post(
            reverse("managed-user-bulk"), {"ids": [suspended.pk], "action": "activate"}, format="json",
        )

        suspended.refresh_from_db()
        self.assertFalse(suspended.is_active)
        self.assertEqual(response.data["failed"], 1)

    def test_suspending_is_never_refused_by_a_full_plan(self):
        """An account can always be taken *out* of a workspace that is over its limit."""
        user_model = get_user_model()
        superadmin_tenant = Tenant.objects.create(name="Platform", kind=Tenant.Kind.PLATFORM)
        superadmin = user_model.objects.create_user(
            username="root", password="pw", tenant=superadmin_tenant, role=user_model.Role.SUPERADMIN,
        )

        client = APIClient()
        client.force_authenticate(superadmin)
        response = client.post(
            reverse("managed-user-bulk"), {"ids": [self.owner.pk], "action": "suspend"}, format="json",
        )

        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)
        self.assertEqual(response.data["succeeded"], 1)


# ---------------------------------------------------------------------------
# Messages (spec §6, §28)
# ---------------------------------------------------------------------------

class MessageAllowanceTests(TestCase):
    def setUp(self):
        from apps.contacts.models import Contact
        from apps.whatsapp.models import WhatsAppAccount

        self.tenant = customer_on(make_plan("Tiny", max_messages_per_month=2))
        self.user = get_user_model().objects.create_user(
            username="owner", password="pw", tenant=self.tenant, credits=1000,
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line", phone_number="+1999",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, phone="+12345678001", created_by=self.user,
        )

    def _message(self, **overrides):
        from apps.messaging.models import Message

        fields = {
            "tenant": self.tenant, "whatsapp_account": self.account, "contact": self.contact,
            "to_phone": self.contact.phone, "body": "hello", "billed_to": self.user,
            "kind": Message.Kind.SERVICE,
        }
        fields.update(overrides)
        return Message.objects.create(**fields)

    def test_a_queued_message_does_not_count_until_it_is_authorised(self):
        """
        The bug this replaces: usage counted every QUEUED row, so a plan
        allowing 100 messages let a campaign of 100 send none of them.
        """
        for _ in range(5):
            self._message()

        self.assertEqual(services.messages_used(self.tenant.pk), 0)
        allowed, _ = services.check_quota(self.tenant.pk, "messages_per_month")
        self.assertTrue(allowed)

    def test_claiming_takes_one_off_the_allowance(self):
        message = self._message()

        ok, _ = services.claim_message_allowance(message)

        self.assertTrue(ok)
        self.assertIsNotNone(message.plan_counted_at)
        self.assertEqual(services.messages_used(self.tenant.pk), 1)

    def test_claiming_twice_costs_one(self):
        """A held message that comes back must not pay for itself again."""
        message = self._message()

        services.claim_message_allowance(message)
        services.claim_message_allowance(message)

        self.assertEqual(services.messages_used(self.tenant.pk), 1)

    def test_the_message_after_the_last_one_is_refused(self):
        for _ in range(2):
            self.assertTrue(services.claim_message_allowance(self._message())[0])

        ok, reason = services.claim_message_allowance(self._message())

        self.assertFalse(ok)
        self.assertIn("Tiny", reason)

    def test_a_released_slot_can_be_used_by_another_message(self):
        first = self._message()
        services.claim_message_allowance(first)
        services.claim_message_allowance(self._message())

        services.release_message_allowance(first)

        self.assertEqual(services.messages_used(self.tenant.pk), 1)
        self.assertTrue(services.claim_message_allowance(self._message())[0])

    def test_a_new_period_restores_the_allowance(self):
        for _ in range(2):
            services.claim_message_allowance(self._message())
        self.assertFalse(services.check_quota(self.tenant.pk, "messages_per_month")[0])

        subscription = self.tenant.subscription
        subscription.renew()

        self.assertEqual(services.messages_used(self.tenant.pk), 0)
        self.assertTrue(services.check_quota(self.tenant.pk, "messages_per_month")[0])

    def test_a_definitely_failed_send_gives_the_slot_back(self):
        """A message the provider definitively refused must not cost a month's quota."""
        from apps.messaging.models import Message
        from apps.tenants.models import TenantSettings
        from apps.whatsapp.providers.base import SendResult
        from apps.messaging.tasks import send_message_task

        # Retries off, so the task gives up on a rejection rather than retrying.
        TenantSettings.objects.update_or_create(
            tenant=self.tenant, defaults={"retry_enabled": False},
        )
        message = self._message()
        with patch("apps.messaging.tasks.ensure_sendable_account", return_value=self.account), \
             patch(
                 "apps.messaging.services.ConnectionManager.send_text_message",
                 return_value=SendResult(success=False, error="not on WhatsApp", code="invalid_recipient"),
             ):
            send_message_task(message.pk)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.FAILED)
        self.assertIsNone(message.plan_counted_at)
        self.assertEqual(services.messages_used(self.tenant.pk), 0)

    def test_an_ambiguous_send_holds_the_slot(self):
        """
        An ambiguous send (the provider was reached but never confirmed) is
        RECOVERY_REQUIRED, not FAILED. We do not know it never reached anybody,
        so the slot is held -- exactly like the credit -- until a webhook or an
        operator settles it.
        """
        from apps.messaging.models import Message
        from apps.whatsapp.providers.base import SendResult
        from apps.messaging.tasks import send_message_task

        message = self._message()
        with patch("apps.messaging.tasks.ensure_sendable_account", return_value=self.account), \
             patch(
                 "apps.messaging.services.ConnectionManager.send_text_message",
                 return_value=SendResult(success=False, ambiguous=True, error="provider never answered"),
             ):
            send_message_task(message.pk)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.RECOVERY_REQUIRED)
        self.assertIsNotNone(message.plan_counted_at)
        self.assertEqual(services.messages_used(self.tenant.pk), 1)

    def test_a_blocked_send_gives_the_slot_back(self):
        from apps.messaging.models import Message
        from apps.messaging.tasks import send_message_task

        self.contact.subscribed = False
        self.contact.save(update_fields=["subscribed"])
        message = self._message(kind=Message.Kind.MARKETING)

        with patch("apps.messaging.tasks.ensure_sendable_account", return_value=self.account):
            send_message_task(message.pk)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.SKIPPED)
        self.assertIsNone(message.plan_counted_at)

    def test_credits_never_buy_past_the_plan(self):
        """Spec §28: both gates, and one cannot stand in for the other."""
        from apps.campaigns.models import Campaign
        from apps.campaigns.services import CampaignError, launch

        self.user.credits = 100_000
        self.user.save(update_fields=["credits"])
        for index in range(5):
            from apps.contacts.models import Contact

            Contact.objects.create(
                tenant=self.tenant, phone=f"+1888888{index:04d}", created_by=self.user,
            )

        campaign = Campaign.objects.create(
            tenant=self.tenant, name="Blast", created_by=self.user, whatsapp_account=self.account,
            message_body="hi", audience_type=Campaign.AudienceType.ALL,
        )

        with self.assertRaises(CampaignError) as caught:
            launch(campaign)

        self.assertIn("Tiny", str(caught.exception))
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.DRAFT)

    def test_an_expired_subscription_cannot_send(self):
        subscription = self.tenant.subscription
        subscription.status = Subscription.Status.CANCELLED
        subscription.current_period_start = timezone.now() - timezone.timedelta(days=60)
        subscription.current_period_end = timezone.now() - timezone.timedelta(days=30)
        subscription.save()

        ok, reason = services.claim_message_allowance(self._message())

        self.assertFalse(ok)
        self.assertIn("cancelled", reason.lower())

    def test_a_broken_allowance_lookup_refuses_the_send(self):
        with patch.object(services, "messages_used", side_effect=RuntimeError("down")):
            ok, reason = services.claim_message_allowance(self._message())

        self.assertFalse(ok)
        self.assertIn("try again", reason.lower())


# ---------------------------------------------------------------------------
# Features (spec §11-§14, §36)
# ---------------------------------------------------------------------------

class FeatureEnforcementTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.starter = make_plan("Starter")
        self.growth = make_plan("Growth")
        self.tenant = customer_on(self.starter)
        self.user = get_user_model().objects.create_user(
            username="owner", password="pw", tenant=self.tenant,
        )
        self.client.force_authenticate(self.user)

    def _upgrade(self):
        subscription = self.tenant.subscription
        subscription.plan = self.growth
        subscription.save(update_fields=["plan"])

    # -- AI agent -----------------------------------------------------------

    def test_the_ai_config_is_readable_but_not_writable_without_the_feature(self):
        self.assertEqual(self.client.get(reverse("ai-agent-config")).status_code, 200)

        response = self.client.patch(reverse("ai-agent-config"), {"is_enabled": True}, format="json")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "FEATURE_NOT_AVAILABLE")
        self.assertEqual(response.data["feature"], "ai_agent")
        self.assertIn("Starter", response.data["detail"])

    def test_the_ai_config_is_writable_once_the_plan_includes_it(self):
        self._upgrade()

        response = self.client.patch(reverse("ai-agent-config"), {"is_enabled": True}, format="json")

        self.assertEqual(response.status_code, 200)

    def test_asking_the_knowledge_base_is_refused_without_the_feature(self):
        response = self.client.post(reverse("knowledge-ask"), {"question": "hours?"}, format="json")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["feature"], "ai_agent")

    def test_an_inbound_reply_is_not_generated_without_the_feature(self):
        """The Celery path, not the view: this is how the agent actually speaks."""
        from apps.ai import services as ai_services

        conversation = type("C", (), {"tenant_id": self.tenant.pk, "pk": 1})()
        message = type("M", (), {"conversation": conversation, "pk": 1})()

        with patch.object(ai_services, "is_configured", return_value=True), \
             patch("apps.ai.tasks.generate_reply_task.delay") as queued:
            self.assertFalse(ai_services.maybe_reply(message))

        queued.assert_not_called()

    def test_answering_is_refused_at_execution_time(self):
        """A task queued while the plan allowed it must not run on the old plan."""
        from apps.ai import services as ai_services

        conversation = type("C", (), {"tenant_id": self.tenant.pk, "pk": 1})()

        with patch.object(ai_services.AIAgentConfig, "for_tenant") as config:
            self.assertFalse(ai_services.answer(conversation, inbound_message=None))

        config.assert_not_called()

    def test_a_previously_configured_agent_stops_when_the_plan_changes(self):
        from apps.ai import services as ai_services

        self._upgrade()
        self.assertTrue(ai_services.agent_available(self.tenant.pk))

        subscription = self.tenant.subscription
        subscription.plan = self.starter
        subscription.save(update_fields=["plan"])

        self.assertFalse(ai_services.agent_available(self.tenant.pk))

    # -- Workflows ----------------------------------------------------------

    def _account(self):
        from apps.whatsapp.models import WhatsAppAccount

        if not hasattr(self, "_cached_account"):
            self._cached_account = WhatsAppAccount.objects.create(
                tenant=self.tenant, user=self.user, name="Line", phone_number="+1999",
                status=WhatsAppAccount.Status.CONNECTED,
            )
        return self._cached_account

    WORKFLOW_NODES = [
        {"id": "t", "type": "TRIGGER", "trigger": {"event": "INBOUND_MESSAGE"}, "next": "m"},
        {"id": "m", "type": "MESSAGE", "body": "hi"},
    ]

    def _workflow_payload(self, name="Seq"):
        return {
            "name": name,
            "whatsapp_account": self._account().pk,
            "nodes": self.WORKFLOW_NODES,
            "is_active": True,
        }

    def test_creating_a_workflow_is_refused_without_the_feature(self):
        response = self.client.post(reverse("workflow-list"), self._workflow_payload(), format="json")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["feature"], "workflows")

    def test_creating_a_workflow_succeeds_once_the_plan_includes_it(self):
        self._upgrade()

        response = self.client.post(reverse("workflow-list"), self._workflow_payload(), format="json")

        self.assertEqual(response.status_code, 201)

    def test_an_old_workflow_cannot_be_edited_or_reactivated_after_a_downgrade(self):
        from apps.automation.models import Workflow

        self._upgrade()
        created = self.client.post(reverse("workflow-list"), self._workflow_payload(), format="json")
        workflow_id = created.data["id"]

        Workflow.objects.filter(pk=workflow_id).update(is_active=False)
        subscription = self.tenant.subscription
        subscription.plan = self.starter
        subscription.save(update_fields=["plan"])

        patched = self.client.patch(
            reverse("workflow-detail", args=[workflow_id]), {"is_active": True}, format="json",
        )
        # Bulk reports per object, the way every bulk endpoint in this
        # codebase does, so the refusal arrives as a failed row rather than a
        # status on the request.
        bulk = self.client.post(
            reverse("workflow-bulk"), {"ids": [workflow_id], "action": "activate"}, format="json",
        )

        self.assertEqual(patched.status_code, 403)
        self.assertEqual(bulk.data["succeeded"], 0)
        self.assertEqual(bulk.data["failed"], 1)
        self.assertIn("Starter", bulk.data["errors"][0]["detail"])
        self.assertFalse(Workflow.objects.get(pk=workflow_id).is_active)

    def test_an_old_workflow_cannot_be_run_after_a_downgrade(self):
        from apps.automation.models import Workflow
        from apps.automation.services import WorkflowError, start_workflow
        from apps.contacts.models import Contact

        workflow = Workflow.objects.create(
            tenant=self.tenant, name="Seq", is_active=True, whatsapp_account=self._account(),
            nodes=self.WORKFLOW_NODES,
        )
        contact = Contact.objects.create(
            tenant=self.tenant, phone="+12345678001", created_by=self.user,
        )

        with self.assertRaises(WorkflowError) as caught:
            start_workflow(workflow, contact)

        self.assertIn("Starter", str(caught.exception))

    def test_a_workflow_still_lists_and_deletes_without_the_feature(self):
        """A customer keeps what they built, and must be able to tidy it up."""
        from apps.automation.models import Workflow

        workflow = Workflow.objects.create(
            tenant=self.tenant, name="Seq", whatsapp_account=self._account(),
            nodes=self.WORKFLOW_NODES,
        )

        self.assertEqual(self.client.get(reverse("workflow-list")).status_code, 200)
        self.assertEqual(
            self.client.delete(reverse("workflow-detail", args=[workflow.pk])).status_code, 204,
        )

    # -- Click tracking -----------------------------------------------------

    def test_links_are_not_rewritten_without_the_feature(self):
        from apps.tracking.services import rewrite

        with override_settings(LINK_TRACKING_ENABLED=True, LINK_TRACKING_BASE_URL="https://example.test"):
            text, links = rewrite("see https://shop.example/x", tenant_id=self.tenant.pk)

        self.assertEqual(text, "see https://shop.example/x")
        self.assertEqual(links, [])

    def test_links_are_rewritten_once_the_plan_includes_it(self):
        from apps.tracking.services import rewrite

        self._upgrade()
        with override_settings(LINK_TRACKING_ENABLED=True, LINK_TRACKING_BASE_URL="https://example.test"):
            text, links = rewrite("see https://shop.example/x", tenant_id=self.tenant.pk)

        self.assertEqual(len(links), 1)
        self.assertIn("https://example.test/r/", text)

    def test_a_campaign_without_tracking_still_sends(self):
        """Spec §13: normal campaign sending must not break."""
        from apps.campaigns.models import Campaign
        from apps.campaigns.services import _build_messages
        from apps.contacts.models import Contact
        from apps.messaging.models import Message
        from apps.tracking.models import TrackedLink

        account = self._account()
        Contact.objects.create(tenant=self.tenant, phone="+12345678001", created_by=self.user)
        campaign = Campaign.objects.create(
            tenant=self.tenant, name="C", created_by=self.user, whatsapp_account=account,
            message_body="visit https://shop.example/x", audience_type=Campaign.AudienceType.ALL,
            track_links=True,
        )

        with override_settings(LINK_TRACKING_ENABLED=True, LINK_TRACKING_BASE_URL="https://example.test"):
            created = _build_messages(campaign)

        self.assertEqual(created, 1)
        self.assertEqual(TrackedLink.objects.filter(tenant=self.tenant).count(), 0)
        self.assertIn("https://shop.example/x", Message.objects.get(campaign=campaign).body)

    # -- API access ---------------------------------------------------------

    def test_the_api_access_permission_refuses_and_explains(self):
        from apps.subscriptions.permissions import RequiresApiAccess

        request = type("R", (), {"user": self.user})()
        permission = RequiresApiAccess()

        self.assertFalse(permission.has_permission(request, view=None))
        self.assertIn("API access", permission.message)

    def test_the_api_access_permission_allows_a_plan_that_includes_it(self):
        from apps.subscriptions.permissions import RequiresApiAccess

        subscription = self.tenant.subscription
        subscription.plan = make_plan("Enterprise")
        subscription.save(update_fields=["plan"])

        request = type("R", (), {"user": self.user})()
        self.assertTrue(RequiresApiAccess().has_permission(request, view=None))


# ---------------------------------------------------------------------------
# The usage endpoint (spec §17, §18)
# ---------------------------------------------------------------------------

class UsageEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = customer_on(make_plan("Growth"))
        self.user = get_user_model().objects.create_user(
            username="owner", password="pw", tenant=self.tenant,
        )
        self.client.force_authenticate(self.user)

    def test_it_reports_plan_subscription_usage_limits_and_features(self):
        response = self.client.get(reverse("my-usage"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["plan"]["name"], "Growth")
        self.assertEqual(response.data["subscription"]["status"], "ACTIVE")
        self.assertEqual(response.data["subscription"]["access"], "FULL")
        self.assertIsNotNone(response.data["subscription"]["current_period_end"])
        for key in ("messages", "contacts", "devices", "users"):
            block = response.data["usage"][key]
            self.assertIn("used", block)
            self.assertIn("limit", block)
            self.assertIn("remaining", block)
            self.assertIn("percentage", block)
        self.assertEqual(
            response.data["features"],
            {
                "ai_agent": True, "workflows": True, "api_access": False, "click_tracking": True,
                # Unlisted on the plan, so on -- see Plan.allows().
                "number_filter": True, "group_grabber": True,
            },
        )

    def test_it_exposes_no_other_tenants_billing(self):
        """404, not 403: a refusal that distinguishes the two is an id oracle."""
        stranger = customer_on(make_plan("Enterprise"), name="Stranger")

        response = self.client.get(reverse("tenant-usage", args=[stranger.pk]))

        self.assertIn(response.status_code, (403, 404))
        self.assertNotIn("Enterprise", str(response.data))

    def test_a_read_failure_degrades_rather_than_taking_the_page_down(self):
        """Spec §15: display is not the security boundary and must not fail closed."""
        with patch.object(services, "usage", side_effect=RuntimeError("counting is hard")):
            response = self.client.get(reverse("my-usage"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["usage_available"])

    def test_usage_counts_are_aggregates_not_row_loads(self):
        """Spec §42: four counts, no table scan into memory."""
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connections["default"]) as queries:
            services.usage(self.tenant.pk)

        # One lookup for the subscription, then one COUNT per metric. What
        # must never appear is a SELECT of the rows themselves.
        self.assertLessEqual(len(queries), 6)
        self.assertGreaterEqual(sum("COUNT" in query["sql"].upper() for query in queries), 4)


# ---------------------------------------------------------------------------
# Plan management (spec §32)
# ---------------------------------------------------------------------------

class PlanValidationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        user_model = get_user_model()
        self.reseller_tenant = Tenant.objects.create(name="Reseller", kind=Tenant.Kind.RESELLER)
        self.reseller = user_model.objects.create_user(
            username="reseller", password="pw", tenant=self.reseller_tenant,
            role=user_model.Role.RESELLER,
        )
        self.client.force_authenticate(self.reseller)

    def _post(self, **fields):
        payload = {"name": "P", "price_cents": 100}
        payload.update(fields)
        return self.client.post(reverse("plan-list"), payload, format="json")

    def test_a_negative_limit_is_refused(self):
        self.assertEqual(self._post(max_contacts=-1).status_code, 400)

    def test_a_negative_price_is_refused(self):
        self.assertEqual(self._post(price_cents=-1).status_code, 400)

    def test_an_absurd_limit_is_refused(self):
        response = self._post(max_contacts=10**12)
        self.assertEqual(response.status_code, 400)
        self.assertIn("max_contacts", response.data)

    def test_an_invalid_interval_is_refused(self):
        self.assertEqual(self._post(interval="FORTNIGHTLY").status_code, 400)

    def test_an_invalid_currency_is_refused(self):
        self.assertEqual(self._post(currency="dollars").status_code, 400)

    def test_a_blank_name_is_refused(self):
        self.assertEqual(self._post(name="   ").status_code, 400)

    def test_a_valid_plan_is_stored_with_its_currency_normalised(self):
        response = self._post(name="Growth", currency="usd")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Plan.objects.get(name="Growth").currency, "USD")


class ResellerAndSuperadminTests(TestCase):
    """Spec §25, §26: the hierarchy still holds, and operators are not customers."""

    def setUp(self):
        user_model = get_user_model()
        self.platform = Tenant.objects.create(name="Platform", kind=Tenant.Kind.PLATFORM)
        self.reseller_tenant = Tenant.objects.create(
            name="Reseller A", kind=Tenant.Kind.RESELLER, parent=self.platform,
        )
        self.rival_tenant = Tenant.objects.create(
            name="Reseller B", kind=Tenant.Kind.RESELLER, parent=self.platform,
        )
        self.customer = Tenant.objects.create(
            name="Customer", kind=Tenant.Kind.CUSTOMER, parent=self.reseller_tenant,
        )
        self.superadmin = user_model.objects.create_user(
            username="root", password="pw", tenant=self.platform, role=user_model.Role.SUPERADMIN,
        )
        self.reseller = user_model.objects.create_user(
            username="reseller", password="pw", tenant=self.reseller_tenant,
            role=user_model.Role.RESELLER,
        )
        self.client = APIClient()

    def test_a_reseller_assigns_their_own_plan_to_their_own_customer(self):
        plan = make_plan("Growth", owner=self.reseller_tenant)
        self.client.force_authenticate(self.reseller)

        response = self.client.post(
            reverse("subscription-list"), {"tenant": self.customer.pk, "plan": plan.pk}, format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(services.feature_enabled(self.customer.pk, "workflows"))

    def test_a_reseller_cannot_move_a_rivals_customer_onto_their_plan(self):
        plan = make_plan("Growth", owner=self.reseller_tenant)
        stranger = Tenant.objects.create(
            name="Stranger", kind=Tenant.Kind.CUSTOMER, parent=self.rival_tenant,
        )
        self.client.force_authenticate(self.reseller)

        response = self.client.post(
            reverse("subscription-list"), {"tenant": stranger.pk, "plan": plan.pk}, format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Subscription.objects.filter(tenant=stranger).exists())

    def test_a_customer_cannot_put_themselves_on_a_better_plan(self):
        user_model = get_user_model()
        plan = make_plan("Enterprise", owner=self.reseller_tenant)
        member = user_model.objects.create_user(
            username="member", password="pw", tenant=self.customer, role=user_model.Role.USER,
        )
        self.client.force_authenticate(member)

        response = self.client.post(
            reverse("subscription-list"), {"tenant": self.customer.pk, "plan": plan.pk}, format="json",
        )

        self.assertEqual(response.status_code, 403)

    def test_a_superadmin_is_never_limited_by_a_customer_plan(self):
        for metric in services.METRICS:
            allowed, _ = services.check_quota(self.platform.pk, metric, 10_000)
            self.assertTrue(allowed, metric)
        for feature in Plan.FEATURES:
            self.assertTrue(services.feature_enabled(self.platform.pk, feature), feature)

    def test_a_superadmin_can_still_manage_plans_and_subscriptions(self):
        self.client.force_authenticate(self.superadmin)
        created = self.client.post(
            reverse("plan-list"), {"name": "Platform Starter", "price_cents": 0}, format="json",
        )
        assigned = self.client.post(
            reverse("subscription-list"),
            {"tenant": self.customer.pk, "plan": created.data["id"]}, format="json",
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(assigned.status_code, 201)


class DefaultPlanCommandTests(TestCase):
    def test_it_subscribes_only_customer_tenants_that_have_none(self):
        from io import StringIO

        from django.core.management import call_command

        plan = make_plan("Starter", owner=None)
        already = customer_on(make_plan("Growth", owner=None), name="Already on a plan")
        fresh = Tenant.objects.create(name="Fresh", kind=Tenant.Kind.CUSTOMER)
        reseller = Tenant.objects.create(name="Reseller", kind=Tenant.Kind.RESELLER)

        call_command("assign_default_plan", plan="Starter", stdout=StringIO())

        self.assertEqual(Subscription.objects.get(tenant=fresh).plan_id, plan.pk)
        self.assertEqual(Subscription.objects.get(tenant=already).plan.name, "Growth")
        self.assertFalse(Subscription.objects.filter(tenant=reseller).exists())

    def test_a_dry_run_writes_nothing(self):
        from io import StringIO

        from django.core.management import call_command

        make_plan("Starter", owner=None)
        Tenant.objects.create(name="Fresh", kind=Tenant.Kind.CUSTOMER)

        call_command("assign_default_plan", plan="Starter", dry_run=True, stdout=StringIO())

        self.assertEqual(Subscription.objects.count(), 0)

    def test_it_refuses_to_guess_a_plan(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("assign_default_plan", plan="Nonexistent", stdout=StringIO())


# ---------------------------------------------------------------------------
# Concurrency (spec §16)
# ---------------------------------------------------------------------------

class ConcurrentQuotaTests(TransactionTestCase):
    """
    Two real connections racing for one free slot.

    TransactionTestCase rather than TestCase because `select_for_update` does
    nothing useful inside the outer transaction TestCase wraps every test in:
    both threads would sit in the same transaction and never contend, and the
    test would pass without testing anything.
    """

    def setUp(self):
        self.plan = Plan.objects.create(
            name="One Each", max_devices=1, max_contacts=1, max_messages_per_month=1,
        )
        self.tenant = Tenant.objects.create(name="Customer", kind=Tenant.Kind.CUSTOMER)
        Subscription.objects.create(tenant=self.tenant, plan=self.plan)
        self.user = get_user_model().objects.create_user(
            username="owner", password="pw", tenant=self.tenant, credits=100,
        )

    def _race(self, action, workers=2):
        """Run `action` on `workers` threads at once, returning what each did."""
        barrier = threading.Barrier(workers)
        outcomes = [None] * workers

        def run(index):
            try:
                barrier.wait(timeout=10)
                outcomes[index] = action(index)
            except Exception as exc:  # noqa: BLE001 - recorded, asserted on below
                outcomes[index] = exc
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(index,)) for index in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return outcomes

    def test_only_one_of_two_simultaneous_devices_is_created(self):
        from apps.whatsapp.models import WhatsAppAccount

        def create(index):
            with services.quota_guard(self.tenant.pk, "devices"):
                WhatsAppAccount.objects.create(
                    tenant=self.tenant, user=self.user, name=f"Phone {index}",
                )
            return "created"

        outcomes = self._race(create)

        self.assertEqual(WhatsAppAccount.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(sum(1 for outcome in outcomes if outcome == "created"), 1)
        self.assertEqual(sum(1 for outcome in outcomes if isinstance(outcome, QuotaExceeded)), 1)

    def test_only_one_of_two_simultaneous_contacts_is_created(self):
        from apps.contacts.models import Contact

        def create(index):
            with services.quota_guard(self.tenant.pk, "contacts"):
                Contact.objects.create(
                    tenant=self.tenant, phone=f"+1300000000{index}", created_by=self.user,
                )
            return "created"

        outcomes = self._race(create)

        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(sum(1 for outcome in outcomes if outcome == "created"), 1)

    def test_only_one_of_two_simultaneous_sends_takes_the_last_slot(self):
        from apps.contacts.models import Contact
        from apps.messaging.models import Message
        from apps.whatsapp.models import WhatsAppAccount

        account = WhatsAppAccount.objects.create(tenant=self.tenant, user=self.user, name="Line")
        contact = Contact.objects.create(
            tenant=self.tenant, phone="+13000000000", created_by=self.user,
        )
        messages = [
            Message.objects.create(
                tenant=self.tenant, whatsapp_account=account, contact=contact,
                to_phone=contact.phone, body="hi", billed_to=self.user,
            )
            for _ in range(2)
        ]

        def claim(index):
            allowed, _ = services.claim_message_allowance(messages[index])
            return allowed

        outcomes = self._race(claim)

        self.assertEqual(sum(1 for outcome in outcomes if outcome is True), 1)
        self.assertEqual(services.messages_used(self.tenant.pk), 1)

    def test_a_failure_inside_the_guard_does_not_eat_the_slot(self):
        from apps.whatsapp.models import WhatsAppAccount

        with self.assertRaises(ValueError):
            with services.quota_guard(self.tenant.pk, "devices"):
                WhatsAppAccount.objects.create(tenant=self.tenant, user=self.user, name="Phone")
                raise ValueError("something else went wrong")

        self.assertEqual(WhatsAppAccount.objects.filter(tenant=self.tenant).count(), 0)
        allowed, _ = services.check_quota(self.tenant.pk, "devices")
        self.assertTrue(allowed)


class DefaultPlanPeriodTests(TestCase):
    """
    A tenant governed by the default plan has no billing anchor.

    Without a period, "messages per month" would mean every message ever sent,
    and such a tenant would be blocked permanently the first time they crossed
    the limit rather than for the rest of one month.
    """

    def setUp(self):
        from apps.contacts.models import Contact
        from apps.whatsapp.models import WhatsAppAccount

        self.tenant = Tenant.objects.create(name="Legacy", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="owner", password="pw", tenant=self.tenant,
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line",
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, phone="+12345678001", created_by=self.user,
        )
        make_plan("Tiny", owner=None, max_messages_per_month=1)

    def _message(self):
        from apps.messaging.models import Message

        return Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, body="hi", billed_to=self.user,
        )

    def test_usage_is_measured_over_the_calendar_month(self):
        with override_settings(BILLING_DEFAULT_PLAN="Tiny"):
            start, end = services.current_period(self.tenant.pk)

        self.assertIsNotNone(start)
        self.assertEqual(start.day, 1)
        self.assertGreater(end, timezone.now())

    def test_last_months_messages_do_not_count_against_this_month(self):
        from apps.messaging.models import Message

        old = self._message()
        Message.objects.filter(pk=old.pk).update(
            plan_counted_at=timezone.now().replace(day=1) - timezone.timedelta(days=5)
        )

        with override_settings(BILLING_DEFAULT_PLAN="Tiny"):
            self.assertEqual(services.messages_used(self.tenant.pk), 0)
            allowed, _ = services.check_quota(self.tenant.pk, "messages_per_month")

        self.assertTrue(allowed)

    def test_the_ceiling_still_applies_within_the_month(self):
        with override_settings(BILLING_DEFAULT_PLAN="Tiny"):
            self.assertTrue(services.claim_message_allowance(self._message())[0])
            allowed, reason = services.claim_message_allowance(self._message())

        self.assertFalse(allowed)
        self.assertIn("Tiny", reason)

    def test_a_tenant_on_no_plan_has_no_period_and_no_ceiling(self):
        start, end = services.current_period(self.tenant.pk)

        self.assertIsNone(start)
        self.assertIsNone(end)
        self.assertTrue(services.check_quota(self.tenant.pk, "messages_per_month")[0])
