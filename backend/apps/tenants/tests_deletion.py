"""
Tenant deletion: the guards, and the honest account of what survives.

This command exists because the alternative -- an operator typing `.delete()`
in a Django shell against production at the end of a long day -- is how the
wrong customer gets deleted. Every test here is one of those guards.
"""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase

from apps.compliance.models import ConsentEvent
from apps.compliance.services import opt_out
from apps.contacts.models import Contact
from apps.legal.models import LegalAcceptance
from apps.legal.services import record_acceptance
from apps.tenants.models import Tenant


class DeletionTestCase(TestCase):
    def setUp(self):
        self.platform = Tenant.objects.create(
            name="Platform", slug="platform-del", kind=Tenant.Kind.PLATFORM,
        )
        self.tenant = Tenant.objects.create(
            name="Acme", slug="acme-del", kind=Tenant.Kind.CUSTOMER, parent=self.platform,
        )
        self.user = get_user_model().objects.create_user(
            username="ada-del", email="ada@del.example",
            password="correct-horse-battery-staple", tenant=self.tenant,
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Bob", phone="+919876543210",
        )

        self.other = Tenant.objects.create(
            name="Rival", slug="rival-del", kind=Tenant.Kind.CUSTOMER, parent=self.platform,
        )
        self.other_user = get_user_model().objects.create_user(
            username="eve-del", email="eve@del.example",
            password="correct-horse-battery-staple", tenant=self.other,
        )
        self.other_contact = Contact.objects.create(
            tenant=self.other, created_by=self.other_user, name="Rival Contact", phone="+919999999999",
        )

    def run_command(self, *args, **options):
        out = StringIO()
        call_command("delete_tenant", *args, stdout=out, stderr=StringIO(), **options)
        return out.getvalue()


class GuardTests(DeletionTestCase):
    def test_nothing_happens_without_the_confirmation(self):
        """
        Not a --yes flag: the operator has to type the identity of the thing
        being destroyed, so a command recalled from shell history points at the
        workspace it names and no other.
        """
        with self.assertRaises(CommandError):
            self.run_command("acme-del")

        self.assertTrue(Contact.objects.filter(pk=self.contact.pk).exists())

    def test_a_wrong_confirmation_is_refused(self):
        with self.assertRaises(CommandError):
            self.run_command("acme-del", confirm="rival-del")

        self.assertTrue(Contact.objects.filter(pk=self.contact.pk).exists())

    def test_the_platform_tenant_cannot_be_deleted(self):
        """That is the deployment itself, not a customer."""
        with self.assertRaises(CommandError) as caught:
            self.run_command("platform-del", confirm="platform-del")
        self.assertIn("platform", str(caught.exception).lower())

    def test_a_tenant_with_children_is_refused(self):
        """Deleting a reseller would take other people's businesses with it."""
        reseller = Tenant.objects.create(
            name="Reseller", slug="reseller-del", kind=Tenant.Kind.RESELLER, parent=self.platform,
        )
        Tenant.objects.create(
            name="Their Customer", slug="their-customer-del",
            kind=Tenant.Kind.CUSTOMER, parent=reseller,
        )

        with self.assertRaises(CommandError) as caught:
            self.run_command("reseller-del", confirm="reseller-del")
        self.assertIn("beneath", str(caught.exception).lower())

    def test_an_unknown_slug_is_refused(self):
        with self.assertRaises(CommandError):
            self.run_command("no-such-tenant", confirm="no-such-tenant")

    def test_a_dry_run_changes_nothing(self):
        output = self.run_command("acme-del", dry_run=True)

        self.assertIn("Would delete", output)
        self.assertTrue(Contact.objects.filter(pk=self.contact.pk).exists())
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.is_active)

    def test_the_dry_run_names_what_is_retained_and_why(self):
        output = self.run_command("acme-del", dry_run=True)

        self.assertIn("RETAINED", output)
        self.assertIn("Consent events", output)
        self.assertIn("Backups", output)
        # A deletion that quietly keeps data is a lie told to whoever asked
        # for it, so the reason is printed alongside each retained category.
        self.assertIn("evidence", output.lower())


class DeletionTests(DeletionTestCase):
    def delete(self):
        return self.run_command("acme-del", confirm="acme-del")

    def test_the_workspaces_own_data_goes(self):
        self.delete()
        self.assertFalse(Contact.objects.filter(pk=self.contact.pk).exists())

    def test_no_other_workspace_is_touched(self):
        """The failure this command exists to make impossible."""
        self.delete()

        self.assertTrue(Contact.objects.filter(pk=self.other_contact.pk).exists())
        self.assertTrue(Tenant.objects.filter(pk=self.other.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.other_user.pk).exists())
        self.other.refresh_from_db()
        self.assertTrue(self.other.is_active)

    def test_the_platform_tenant_survives(self):
        self.delete()
        self.assertTrue(Tenant.objects.filter(pk=self.platform.pk, is_active=True).exists())

    def test_consent_records_are_retained(self):
        """
        They are the evidence that an opt-out was honoured. Destroying them
        destroys the customer's own defence against a complaint about messages
        already sent.
        """
        opt_out(self.contact, source=ConsentEvent.Source.INBOUND_KEYWORD, detail="Replied STOP")
        self.assertTrue(ConsentEvent.objects.filter(tenant=self.tenant).exists())

        self.delete()

        self.assertTrue(ConsentEvent.objects.filter(tenant=self.tenant).exists())

    def test_legal_acceptances_are_retained(self):
        record_acceptance(self.user, "terms")
        self.delete()
        self.assertTrue(LegalAcceptance.objects.filter(tenant=self.tenant).exists())

    def test_the_tenant_row_is_kept_and_marked_inactive(self):
        """
        The retained consent, audit, credit and legal rows point at it.
        Deleting it would either cascade into them or leave them dangling.
        """
        self.delete()

        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.is_active)

    def test_the_deleted_workspace_can_no_longer_send(self):
        """
        Marked inactive is not decoration: the send gate and the authentication
        layer both already treat it as "this workspace may do nothing".
        """
        self.delete()
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.is_active)

    def test_the_output_reports_what_went(self):
        output = self.delete()
        self.assertIn("Deleted", output)
        self.assertIn("contacts", output)
