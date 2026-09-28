"""
The number filter and the group grabber.

Two things are pinned throughout. First, everything these tools *save* goes
through the ordinary contact importer, so the quota, the gap-filling rule and
consent apply unchanged -- the tests assert on ContactImport rows and on
contacts left at UNKNOWN consent, not on any shortcut. Second, WhatsApp is
never asked in a test: the manager's lookup methods are patched, and the
capability flag comes from the real provider class via
`configured_qr_provider`, exactly as the composer suites do.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.contacts.models import Contact, ContactGroup, ContactImport
from apps.subscriptions.models import Subscription
from apps.subscriptions.tests_entitlements import make_plan
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount
from apps.whatsapp.providers.base import GroupMember, GroupMembers, GroupSummary, NumberLookup, ProviderError
from apps.whatsapp.tests_capabilities import configured_qr_provider

from .models import GroupGrab, NumberCheck, NumberCheckEntry
from .services import GROUP_FIELD

CHECKS = "/api/v1/leadtools/number-checks/"
GRABS = "/api/v1/leadtools/group-grabs/"
GROUPS = "/api/v1/leadtools/groups/"


def _qr_account(tenant, user, **overrides):
    defaults = {
        "name": "Sales",
        "connection_method": WhatsAppAccount.ConnectionMethod.QR,
        "status": WhatsAppAccount.Status.CONNECTED,
        "provider_session_id": f"leadtools-{tenant.pk}",
    }
    defaults.update(overrides)
    return WhatsAppAccount.objects.create(tenant=tenant, user=user, **defaults)


class _LeadToolsTestCase(TestCase):
    slug = "acme-leadtools"

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug=self.slug, kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username=f"{self.slug}-owner", password="x", tenant=self.tenant)
        self.account = _qr_account(self.tenant, self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _contact(self, phone, **fields):
        return Contact.objects.create(tenant=self.tenant, created_by=self.user, phone=phone, **fields)


@override_settings(LEADTOOLS_NUMBER_CHECK_PAUSE_SECONDS=0)
class NumberCheckTests(_LeadToolsTestCase):
    slug = "acme-number-filter"

    @staticmethod
    def _answers(registered):
        def check_numbers(account, phones):
            return [NumberLookup(phone=phone, exists=phone in registered) for phone in phones]
        return check_numbers

    def _run(self, payload, registered=()):
        """POST a check with WhatsApp answering "registered" for `registered`, and let the worker run."""
        with configured_qr_provider(), patch(
            "apps.leadtools.tasks.ConnectionManager.check_numbers", side_effect=self._answers(set(registered)),
        ) as mocked, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(CHECKS, payload, format="json")
        return response, mocked

    def _paste(self, text, **extra):
        return {"whatsapp_account": self.account.pk, "source": "PASTE", "text": text, **extra}

    def test_a_pasted_list_is_checked_and_the_contacts_stamped(self):
        priya = self._contact("+15550000001", name="Priya")

        response, mocked = self._run(
            self._paste("+15550000001\n+15550000002\nnot a number"), registered={"+15550000001"},
        )

        self.assertEqual(response.status_code, 201, response.data)
        check = NumberCheck.objects.get(pk=response.data["id"])
        self.assertEqual(check.status, NumberCheck.Status.COMPLETED)
        self.assertEqual(
            (check.total, check.checked, check.on_whatsapp_count, check.not_on_whatsapp_count, check.error_count),
            (2, 2, 1, 1, 0),
        )
        # The unusable line was never asked about: WhatsApp sees only numbers.
        mocked.assert_called_once()
        self.assertEqual(mocked.call_args.args[1], ["+15550000001", "+15550000002"])
        # A number already on the account is linked to its contact, and the
        # contact itself is stamped so the list can be filtered on it.
        self.assertEqual(check.entries.get(phone="+15550000001").contact_id, priya.pk)
        priya.refresh_from_db()
        self.assertEqual(priya.whatsapp_status, Contact.WhatsAppStatus.ON_WHATSAPP)
        self.assertIsNotNone(priya.whatsapp_checked_at)

    def test_results_can_be_listed_by_outcome_and_exported(self):
        response, _ = self._run(self._paste("+15550000011\n+15550000012"), registered={"+15550000011"})
        pk = response.data["id"]

        on = self.client.get(f"{CHECKS}{pk}/entries/?result=ON_WHATSAPP")
        self.assertEqual([row["phone"] for row in on.json()["results"]], ["+15550000011"])

        export = self.client.get(f"{CHECKS}{pk}/export/?result=NOT_ON_WHATSAPP")
        body = b"".join(export.streaming_content).decode()
        self.assertIn("+15550000012", body)
        self.assertNotIn("+15550000011", body)

    def test_saving_a_result_creates_contacts_in_a_group_through_the_importer(self):
        response, _ = self._run(
            self._paste("+15550000021\n+15550000022"), registered={"+15550000021", "+15550000022"},
        )
        pk = response.data["id"]

        with self.captureOnCommitCallbacks(execute=True):
            saved = self.client.post(
                f"{CHECKS}{pk}/save/", {"result": "ON_WHATSAPP", "group_name": "Verified"}, format="json",
            )

        self.assertEqual(saved.status_code, 201, saved.data)
        group = ContactGroup.objects.get(tenant=self.tenant, name="Verified")
        contact_import = ContactImport.objects.get(pk=saved.data["contact_import"]["id"])
        self.assertEqual(contact_import.status, ContactImport.Status.COMPLETED)
        self.assertEqual(contact_import.created_count, 2)
        self.assertEqual(
            set(group.contacts.values_list("phone", flat=True)), {"+15550000021", "+15550000022"},
        )
        # Being on WhatsApp is not consent. Never was, never will be.
        self.assertTrue(all(
            contact.marketing_consent == Contact.MarketingConsent.UNKNOWN for contact in group.contacts.all()
        ))

    def test_checking_existing_contacts_can_be_limited_to_a_group(self):
        vip = ContactGroup.objects.create(tenant=self.tenant, name="VIP")
        self._contact("+15550000031").groups.add(vip)
        self._contact("+15550000032")

        response, mocked = self._run(
            {"whatsapp_account": self.account.pk, "source": "CONTACTS", "contact_group": vip.pk},
            registered={"+15550000031"},
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(mocked.call_args.args[1], ["+15550000031"])
        self.assertEqual(response.data["contact_group_name"], "VIP")

    def test_only_a_connected_qr_number_can_check(self):
        cloud = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Cloud",
            connection_method=WhatsAppAccount.ConnectionMethod.CLOUD_API, status=WhatsAppAccount.Status.CONNECTED,
        )
        with configured_qr_provider():
            refused = self.client.post(CHECKS, self._paste("+15550000041", whatsapp_account=cloud.pk), format="json")
            self.assertEqual(refused.status_code, 400)
            self.assertIn("Cloud API", refused.data["detail"])

            self.account.status = WhatsAppAccount.Status.DISCONNECTED
            self.account.save(update_fields=["status"])
            refused = self.client.post(CHECKS, self._paste("+15550000041"), format="json")
            self.assertEqual(refused.status_code, 400)
            self.assertIn("not connected", refused.data["detail"])
        self.assertFalse(NumberCheck.objects.exists())

    @override_settings(LEADTOOLS_NUMBER_CHECK_MAX_NUMBERS=1)
    def test_a_list_over_the_ceiling_is_refused_before_anything_is_asked(self):
        with configured_qr_provider():
            refused = self.client.post(CHECKS, self._paste("+15550000051\n+15550000052"), format="json")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("at most", refused.data["detail"])
        self.assertFalse(NumberCheck.objects.exists())

    def test_a_provider_failure_finishes_the_check_as_failed_and_keeps_the_unasked_rows_pending(self):
        with configured_qr_provider(), patch(
            "apps.leadtools.tasks.ConnectionManager.check_numbers",
            side_effect=ProviderError("WhatsApp did not answer the lookup.", code="lookup_failed"),
        ), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(CHECKS, self._paste("+15550000061\n+15550000062"), format="json")

        check = NumberCheck.objects.get(pk=response.data["id"])
        self.assertEqual(check.status, NumberCheck.Status.FAILED)
        self.assertIn("did not answer", check.error)
        self.assertEqual(check.entries.filter(result=NumberCheckEntry.Result.PENDING).count(), 2)

    def test_another_workspace_cannot_see_the_check(self):
        response, _ = self._run(self._paste("+15550000071"))
        other = Tenant.objects.create(name="Globex", slug="globex-number-filter", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="globex-number-filter", password="x", tenant=other)
        client = APIClient()
        client.force_authenticate(user=other_user)

        self.assertEqual(client.get(f"{CHECKS}{response.data['id']}/").status_code, 404)
        self.assertEqual(client.get(CHECKS).json()["count"], 0)

    def test_the_plan_feature_gates_the_whole_route(self):
        Subscription.objects.create(tenant=self.tenant, plan=make_plan(features={"number_filter": False}))

        self.assertEqual(self.client.get(CHECKS).status_code, 403)
        with configured_qr_provider():
            self.assertEqual(self.client.post(CHECKS, self._paste("+15550000081"), format="json").status_code, 403)

    def test_it_requires_authentication(self):
        self.assertEqual(APIClient().get(CHECKS).status_code, 401)


class GroupGrabberTests(_LeadToolsTestCase):
    slug = "acme-group-grabber"

    def _grab(self, fetched, payload):
        with configured_qr_provider(), patch(
            "apps.leadtools.services.ConnectionManager.get_group_members", return_value=fetched,
        ) as mocked, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(GRABS, payload, format="json")
        return response, mocked

    def test_groups_are_listed_from_the_linked_number(self):
        summaries = [GroupSummary(id="1@g.us", subject="Traders", member_count=3, announce=True)]
        with configured_qr_provider(), patch(
            "apps.leadtools.services.ConnectionManager.list_groups", return_value=summaries,
        ):
            response = self.client.get(f"{GROUPS}?account={self.account.pk}")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["groups"], [{
            "id": "1@g.us", "subject": "Traders", "member_count": 3,
            "announce": True, "is_community": False, "created_at": None,
        }])

    def test_grabbing_imports_visible_members_once_into_one_contact_group(self):
        fetched = [
            GroupMembers(id="1@g.us", subject="Traders", hidden_count=1, members=[
                GroupMember(phone="+15550000101", name="Asha", is_admin=True),
                GroupMember(phone="+15550000102"),
            ]),
            # Asha is in both groups; she is one contact, from the first.
            GroupMembers(id="2@g.us", subject="Buyers", members=[GroupMember(phone="+15550000101", name="Asha B")]),
        ]

        response, mocked = self._grab(fetched, {"whatsapp_account": self.account.pk, "group_ids": ["1@g.us", "2@g.us"]})

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(mocked.call_args.args[1], ["1@g.us", "2@g.us"])
        grab = GroupGrab.objects.get(pk=response.data["id"])
        self.assertEqual((grab.members_found, grab.members_hidden), (2, 1))
        self.assertEqual(grab.contact_group.name, "WhatsApp groups")
        self.assertEqual([group["subject"] for group in grab.groups], ["Traders", "Buyers"])

        grab.contact_import.refresh_from_db()
        self.assertEqual(grab.contact_import.status, ContactImport.Status.COMPLETED)
        self.assertEqual(grab.contact_import.created_count, 2)
        asha = Contact.objects.get(tenant=self.tenant, phone="+15550000101")
        self.assertEqual(asha.name, "Asha")
        self.assertEqual(asha.custom_fields.get(GROUP_FIELD), "Traders")
        self.assertIn(grab.contact_group, asha.groups.all())
        # Membership of a group is not consent to be marketed to.
        self.assertEqual(asha.marketing_consent, Contact.MarketingConsent.UNKNOWN)

    def test_a_single_group_names_the_contact_group_after_itself(self):
        fetched = [GroupMembers(id="1@g.us", subject="Traders", members=[GroupMember(phone="+15550000111")])]
        response, _ = self._grab(fetched, {"whatsapp_account": self.account.pk, "group_ids": ["1@g.us"]})
        self.assertEqual(response.data["contact_group_name"], "Traders")

    def test_a_chosen_name_wins(self):
        fetched = [GroupMembers(id="1@g.us", subject="Traders", members=[GroupMember(phone="+15550000121")])]
        response, _ = self._grab(
            fetched, {"whatsapp_account": self.account.pk, "group_ids": ["1@g.us"], "contact_group_name": "Leads Q3"},
        )
        self.assertEqual(response.data["contact_group_name"], "Leads Q3")

    def test_a_grab_with_no_visible_numbers_is_refused(self):
        fetched = [GroupMembers(id="1@g.us", subject="Hidden", hidden_count=5)]
        response, _ = self._grab(fetched, {"whatsapp_account": self.account.pk, "group_ids": ["1@g.us"]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("visible phone number", response.data["detail"])
        self.assertFalse(GroupGrab.objects.exists())
        self.assertFalse(ContactImport.objects.exists())

    def test_a_provider_refusal_is_reported_not_swallowed(self):
        with configured_qr_provider(), patch(
            "apps.leadtools.services.ConnectionManager.get_group_members",
            side_effect=ProviderError("This session is not connected.", code="not_connected"),
        ):
            response = self.client.post(GRABS, {"whatsapp_account": self.account.pk, "group_ids": ["1@g.us"]}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "not_connected")

    @override_settings(LEADTOOLS_GROUP_GRAB_MAX_GROUPS=1)
    def test_too_many_groups_are_refused(self):
        with configured_qr_provider():
            response = self.client.post(
                GRABS, {"whatsapp_account": self.account.pk, "group_ids": ["1@g.us", "2@g.us"]}, format="json",
            )
        self.assertEqual(response.status_code, 400)

    def test_the_plan_feature_gates_the_group_grabber(self):
        Subscription.objects.create(tenant=self.tenant, plan=make_plan(features={"group_grabber": False}))
        self.assertEqual(self.client.get(f"{GROUPS}?account={self.account.pk}").status_code, 403)
        self.assertEqual(self.client.get(GRABS).status_code, 403)

    def test_it_requires_authentication(self):
        self.assertEqual(APIClient().get(GROUPS).status_code, 401)
