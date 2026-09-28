"""
The audit trail: that it exists at all, and that it cannot be edited away.

**The regression these exist for.** `log_event` began with a bare
`return None` before any of its body, so every call site in the application --
contact deletion, bulk actions, consent changes, compliance policy edits,
webhook processing -- recorded nothing. The trail looked implemented from every
call site, the admin page existed, and the table stayed empty.

Nothing failed, which is exactly why it survived: an audit trail's absence is
only noticed when somebody asks it a question, and by then the events are gone.
"""

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from rest_framework.test import APIClient

from apps.audit.models import AuditEvent
from apps.audit.services import log_event
from apps.contacts.models import Contact
from apps.tenants.models import Tenant


class LogEventTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-audit", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="ada-audit", email="ada@audit.example",
            password="correct-horse-battery-staple", tenant=self.tenant,
        )
        self.factory = RequestFactory()

    def request(self, **meta):
        request = self.factory.get("/", **meta)
        request.user = self.user
        return request

    def test_an_event_is_actually_written(self):
        event = log_event(
            request=self.request(), action="test.performed", tenant=self.tenant,
            object_type="thing", object_id=7,
        )

        self.assertIsNotNone(event, "log_event returned None -- the trail is dead again.")
        self.assertTrue(AuditEvent.objects.filter(action="test.performed").exists())

    def test_the_actor_comes_from_the_request(self):
        log_event(request=self.request(), action="test.actor", tenant=self.tenant)
        self.assertEqual(AuditEvent.objects.get(action="test.actor").actor_id, self.user.pk)

    def test_an_anonymous_request_records_no_actor_rather_than_failing(self):
        from django.contrib.auth.models import AnonymousUser

        request = self.factory.get("/")
        request.user = AnonymousUser()
        log_event(request=request, action="test.anonymous", tenant=self.tenant)

        self.assertIsNone(AuditEvent.objects.get(action="test.anonymous").actor_id)

    def test_the_client_address_is_taken_from_the_proxy_header(self):
        log_event(
            request=self.request(HTTP_X_FORWARDED_FOR="203.0.113.7, 10.0.0.1"),
            action="test.address", tenant=self.tenant,
        )
        self.assertEqual(AuditEvent.objects.get(action="test.address").ip_address, "203.0.113.7")

    def test_a_missing_tenant_is_skipped_rather_than_raising(self):
        """
        The action being audited already happened by the time this is called,
        so failing the request over a missing trail entry would be worse than
        skipping it.
        """
        self.assertIsNone(log_event(request=self.request(), action="test.no_tenant", tenant=None))

    def test_a_database_failure_never_reaches_the_caller(self):
        from unittest import mock

        with mock.patch.object(
            AuditEvent.objects, "create", side_effect=RuntimeError("table gone"),
        ):
            self.assertIsNone(
                log_event(request=self.request(), action="test.broken", tenant=self.tenant)
            )


class CallSiteTests(TestCase):
    """The trail has to record what the application actually does, not only what a unit test asks it to."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-audit-cs", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="ada-audit-cs", email="ada@auditcs.example",
            password="correct-horse-battery-staple", tenant=self.tenant,
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Bob", phone="+919876543210",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_unsubscribing_a_contact_is_audited(self):
        self.client.post(f"/api/v1/contacts/contacts/{self.contact.pk}/unsubscribe/")
        self.assertTrue(
            AuditEvent.objects.filter(action="contact.unsubscribed", tenant=self.tenant).exists()
        )

    def test_recording_consent_is_audited(self):
        self.client.post(
            f"/api/v1/contacts/contacts/{self.contact.pk}/set-consent/",
            {"granted": True, "source": "WEBSITE"}, format="json",
        )
        self.assertTrue(
            AuditEvent.objects.filter(action="contact.consent_recorded", tenant=self.tenant).exists()
        )

    def test_deleting_a_contact_is_audited(self):
        self.client.delete(f"/api/v1/contacts/contacts/{self.contact.pk}/")
        event = AuditEvent.objects.filter(action="contact.deleted", tenant=self.tenant).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.actor_id, self.user.pk)

    def test_accepting_terms_is_audited(self):
        self.client.post("/api/v1/legal/accept/", {"documents": ["terms"]}, format="json")
        self.assertTrue(AuditEvent.objects.filter(action="legal.accepted", tenant=self.tenant).exists())

    def test_exporting_the_workspace_is_audited(self):
        """A whole-workspace export is the single largest data movement a user can cause."""
        self.client.get("/api/v1/tenants/export/")
        self.assertTrue(AuditEvent.objects.filter(action="tenant.exported", tenant=self.tenant).exists())

    def test_a_successful_sign_in_is_audited(self):
        AuditEvent.objects.all().delete()
        APIClient().post(
            "/api/v1/auth/token/",
            {"username": "ada-audit-cs", "password": "correct-horse-battery-staple"},
            format="json",
        )
        self.assertTrue(AuditEvent.objects.filter(action="auth.login", tenant=self.tenant).exists())


class ImmutabilityTests(TestCase):
    def test_the_admin_permits_neither_add_change_nor_delete(self):
        """
        Add and change were already blocked; delete was not, so an operator
        could remove the record of their own action from the interface that
        records it.
        """
        from django.contrib import admin as django_admin

        from apps.audit.models import AuditEvent as Model

        model_admin = django_admin.site._registry[Model]
        self.assertFalse(model_admin.has_add_permission(None))
        self.assertFalse(model_admin.has_change_permission(None))
        self.assertFalse(model_admin.has_delete_permission(None))
