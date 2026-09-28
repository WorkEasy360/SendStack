"""Release-gate regressions captured by the skeptical product audit.

These tests intentionally use ``expectedFailure`` while the corresponding
production defects are open.  That keeps the failure visible in CI without
turning the audit into a production-code change.  Remove the decorator only
after the implementation and migration are complete; an unexpected success
then makes CI require the test to be promoted to a normal gate.
"""

from unittest import expectedFailure

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.compliance.models import ConsentEvent
from apps.compliance.services import may_receive_marketing, opt_out
from apps.contacts.models import Contact
from apps.contacts.upsert import upsert_contact
from apps.crm.services import merge_contacts
from apps.inbox.models import Conversation
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount


class OptOutLifecycleAuditTest(TestCase):
    """A STOP decision must survive contact deletion and re-import."""

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Audit Consent",
            slug="audit-consent-lifecycle",
            kind=Tenant.Kind.CUSTOMER,
        )
        self.user = get_user_model().objects.create_user(
            username="audit-consent-owner",
            email="audit-consent@example.test",
            password="not-used",
            tenant=self.tenant,
        )

    @expectedFailure
    def test_opt_out_survives_delete_and_reimport(self):
        phone = "+15550001111"
        contact = Contact.objects.create(
            tenant=self.tenant,
            created_by=self.user,
            name="Suppressed",
            phone=phone,
        )
        opt_out(
            contact,
            source=ConsentEvent.Source.INBOUND_KEYWORD,
            detail="STOP in audit regression",
        )
        contact.delete()

        recreated, _ = upsert_contact(
            tenant=self.tenant,
            phone=phone,
            defaults={"created_by": self.user, "name": "Re-imported"},
        )

        self.assertFalse(recreated.subscribed)
        self.assertFalse(may_receive_marketing(recreated)[0])


class CrmMergeAuditTest(TestCase):
    """Merging contacts must not cascade-delete either conversation's messages."""

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Audit CRM",
            slug="audit-crm-merge",
            kind=Tenant.Kind.CUSTOMER,
        )
        self.user = get_user_model().objects.create_user(
            username="audit-crm-owner",
            email="audit-crm@example.test",
            password="not-used",
            tenant=self.tenant,
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant,
            user=self.user,
            name="Audit line",
            phone_number_id="audit-crm-line",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.account.set_access_token("audit-token")
        self.account.set_app_secret("audit-secret")
        self.account.save()

    @expectedFailure
    def test_merge_preserves_messages_from_duplicate_conversation(self):
        primary = Contact.objects.create(
            tenant=self.tenant,
            created_by=self.user,
            name="Primary",
            phone="+15550002221",
        )
        duplicate = Contact.objects.create(
            tenant=self.tenant,
            created_by=self.user,
            name="Duplicate",
            phone="+15550002222",
        )
        primary_thread = Conversation.objects.create(
            tenant=self.tenant,
            whatsapp_account=self.account,
            contact=primary,
        )
        duplicate_thread = Conversation.objects.create(
            tenant=self.tenant,
            whatsapp_account=self.account,
            contact=duplicate,
        )
        Message.objects.create(
            tenant=self.tenant,
            whatsapp_account=self.account,
            contact=primary,
            conversation=primary_thread,
            to_phone=primary.phone,
            direction=Message.Direction.INBOUND,
            body="primary history",
            provider_message_id="audit-primary-message",
            sent_at=timezone.now(),
        )
        Message.objects.create(
            tenant=self.tenant,
            whatsapp_account=self.account,
            contact=duplicate,
            conversation=duplicate_thread,
            to_phone=duplicate.phone,
            direction=Message.Direction.INBOUND,
            body="duplicate history",
            provider_message_id="audit-duplicate-message",
            sent_at=timezone.now(),
        )

        merge_contacts(primary, duplicate)

        self.assertEqual(Message.objects.filter(body__in=["primary history", "duplicate history"]).count(), 2)
        self.assertTrue(Message.objects.filter(body="duplicate history").exists())

