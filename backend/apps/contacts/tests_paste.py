"""
The pasted-list importer.

Two things are being protected here and they pull in opposite directions.

The first is that **nothing is created until somebody presses Import**. Every
test that calls `parse` asserts the contact table is untouched, because the
whole value of the review table is that a list with four typos in it gets
corrected before it becomes four contacts nobody can message.

The second is that a paste must not silently invent a country code. A bare
ten-digit Indian mobile is a perfectly valid E.164 number in Iran, so guessing
produces a number that passes every check and belongs to a stranger. The
importer refuses instead, by name, and the reviewer supplies the code once.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant

from .models import Contact
from .paste import (
    DUPLICATE,
    EMPTY,
    EXISTING,
    INVALID,
    NEW,
    NO_COUNTRY_CODE,
    UNSUBSCRIBED,
    parse,
    summarise,
)


class ParseFormatTests(TestCase):
    """The two formats people actually paste, and the mess around them."""

    def _statuses(self, text, **kwargs):
        return [(row["name"], row["phone"], row["status"]) for row in parse(text, **kwargs)]

    def test_a_bare_number_per_line(self):
        rows = self._statuses("+919876543210\n+919876543211", default_country_code="91")

        self.assertEqual(rows, [("", "+919876543210", NEW), ("", "+919876543211", NEW)])

    def test_name_then_number(self):
        rows = self._statuses("Aman,9876543210\nRahul,9876543211", default_country_code="91")

        self.assertEqual(rows, [("Aman", "+919876543210", NEW), ("Rahul", "+919876543211", NEW)])

    def test_number_then_name_is_read_the_same_way(self):
        """
        Which field is which is decided by which one looks like a number, not
        by position -- people paste both orders, and assuming one would import
        half a customer's list with the numbers in the name column.
        """
        rows = self._statuses("9876543210,Aman", default_country_code="91")

        self.assertEqual(rows, [("Aman", "+919876543210", NEW)])

    def test_tabs_semicolons_pipes_and_dashes_all_separate(self):
        text = "Aman\t9876543210\nRahul;9876543211\nPriya|9876543212\nSana - 9876543213"

        rows = self._statuses(text, default_country_code="91")

        self.assertEqual([name for name, _, _ in rows], ["Aman", "Rahul", "Priya", "Sana"])
        self.assertTrue(all(status == NEW for _, _, status in rows))

    def test_spreadsheet_quoting_is_stripped(self):
        rows = self._statuses('"Aman","9876543210"', default_country_code="91")

        self.assertEqual(rows, [("Aman", "+919876543210", NEW)])

    def test_blank_lines_are_dropped_not_reported(self):
        """People paste lists with spacing in them; that is not a mistake."""
        rows = parse("9876543210\n\n   \n9876543211", default_country_code="91")

        self.assertEqual(len(rows), 2)

    def test_a_line_with_no_number_at_all_is_reported(self):
        rows = self._statuses("Aman\n9876543210", default_country_code="91")

        self.assertEqual(rows[0][2], EMPTY)

    def test_punctuation_inside_a_number_is_tolerated(self):
        rows = self._statuses("+91 (98765) 43210", default_country_code="91")

        self.assertEqual(rows[0][1], "+919876543210")

    def test_a_00_prefix_is_read_as_international(self):
        rows = self._statuses("00919876543210", default_country_code="1")

        self.assertEqual(rows[0][1], "+919876543210")


class CountryCodeTests(TestCase):
    def test_a_bare_number_with_no_default_is_refused_rather_than_guessed(self):
        rows = parse("9876543210")

        self.assertEqual(rows[0]["status"], NO_COUNTRY_CODE)
        self.assertEqual(rows[0]["phone"], "")
        self.assertFalse(rows[0]["importable"])

    def test_the_default_is_applied_when_one_is_given(self):
        rows = parse("9876543210", default_country_code="91")

        self.assertEqual(rows[0]["phone"], "+919876543210")

    def test_a_number_that_already_carries_the_code_is_not_prefixed_twice(self):
        rows = parse("919876543210", default_country_code="91")

        self.assertEqual(rows[0]["phone"], "+919876543210")

    def test_a_national_number_that_merely_starts_with_the_code_still_gets_it(self):
        """
        "919191919" is nine digits beginning with 91. Treating that as
        already-prefixed would silently drop its first two digits.
        """
        rows = parse("919191919", default_country_code="91")

        self.assertEqual(rows[0]["phone"], "+91919191919")

    def test_an_explicit_plus_is_never_touched_by_the_default(self):
        rows = parse("+14155552671", default_country_code="91")

        self.assertEqual(rows[0]["phone"], "+14155552671")

    def test_nonsense_is_invalid_rather_than_country_code_missing(self):
        rows = parse("+1", default_country_code="91")

        self.assertEqual(rows[0]["status"], INVALID)


class ClassificationTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Paste Co", slug="paste-co", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="paste-owner", password="x", tenant=self.tenant,
        )

    def test_a_repeat_inside_the_paste_is_a_duplicate(self):
        rows = parse("9876543210\n9876543210", default_country_code="91")

        self.assertEqual([row["status"] for row in rows], [NEW, DUPLICATE])

    def test_a_number_already_on_the_account_is_existing(self):
        rows = parse("9876543210", default_country_code="91", existing={"+919876543210"})

        self.assertEqual(rows[0]["status"], EXISTING)
        # Importable: a reviewer may legitimately want the name filled in.
        self.assertTrue(rows[0]["importable"])

    def test_an_unsubscribed_contact_is_called_out_ahead_of_merely_existing(self):
        """
        An unsubscribed contact *is* an existing one. Which of the two the
        reviewer is told decides whether they understand that importing this
        row will not make it messageable.
        """
        rows = parse(
            "9876543210", default_country_code="91",
            existing={"+919876543210"}, unsubscribed={"+919876543210"},
        )

        self.assertEqual(rows[0]["status"], UNSUBSCRIBED)

    def test_the_summary_counts_every_status(self):
        rows = parse(
            "Aman,9876543210\n9876543210\n+1\nRahul\n9876543299",
            default_country_code="91",
        )

        summary = summarise(rows)

        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["new"], 2)
        self.assertEqual(summary["duplicates"], 1)
        # "Rahul" is a line with content and no number; "+1" is digits that
        # cannot be a number. Different mistakes, counted separately.
        self.assertEqual(summary["empty"], 1)
        self.assertEqual(summary["invalid"], 1)
        self.assertEqual(summary["importable"], 3)


class PasteApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Paste API", slug="paste-api", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="paste-api-owner", password="x", tenant=self.tenant,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_parse_creates_nothing(self):
        """The whole point of the review step."""
        response = self.client.post(
            "/api/v1/contacts/imports/parse/",
            {"text": "Aman,9876543210\nRahul,9876543211", "default_country_code": "91"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["new"], 2)
        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 0)

    def test_parse_reports_contacts_this_workspace_already_has(self):
        Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+919876543210", name="Aman",
        )

        response = self.client.post(
            "/api/v1/contacts/imports/parse/",
            {"text": "9876543210\n9876543211", "default_country_code": "91"},
            format="json",
        )

        statuses = [row["status"] for row in response.json()["rows"]]
        self.assertEqual(statuses, [EXISTING, NEW])

    def test_parse_does_not_see_another_workspaces_contacts(self):
        other = Tenant.objects.create(name="Other", slug="paste-other", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(
            username="paste-other-owner", password="x", tenant=other,
        )
        Contact.objects.create(tenant=other, created_by=other_user, phone="+919876543210")

        response = self.client.post(
            "/api/v1/contacts/imports/parse/",
            {"text": "9876543210", "default_country_code": "91"},
            format="json",
        )

        self.assertEqual(response.json()["rows"][0]["status"], NEW)

    def test_import_creates_the_reviewed_rows(self):
        response = self.client.post(
            "/api/v1/contacts/imports/paste/",
            {"rows": [{"name": "Aman", "phone": "+919876543210"}, {"name": "", "phone": "+919876543211"}]},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["created"], 2)
        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 2)
        self.assertEqual(
            Contact.objects.get(tenant=self.tenant, phone="+919876543210").name, "Aman",
        )

    def test_importing_an_existing_contact_fills_a_gap_rather_than_overwriting(self):
        Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+919876543210", name="Aman Gupta",
        )

        response = self.client.post(
            "/api/v1/contacts/imports/paste/",
            {"rows": [{"name": "aman", "phone": "+919876543210"}]},
            format="json",
        )

        self.assertEqual(response.json()["created"], 0)
        self.assertEqual(
            Contact.objects.get(tenant=self.tenant, phone="+919876543210").name, "Aman Gupta",
        )

    def test_importing_never_resubscribes_somebody_who_opted_out(self):
        """
        The precedence rule: an explicit opt-out outranks any later list that
        claims consent. Enforced in compliance.set_marketing_consent, asserted
        here because a paste is the easiest way to try to defeat it.
        """
        contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+919876543210", subscribed=False,
        )

        self.client.post(
            "/api/v1/contacts/imports/paste/",
            {
                "rows": [{"name": "Aman", "phone": "+919876543210"}],
                "consent_source": "CSV",
                "consent_reference": "list-2026",
            },
            format="json",
        )

        contact.refresh_from_db()
        self.assertFalse(contact.subscribed)

    def test_a_number_the_server_cannot_normalise_is_reported_not_created(self):
        response = self.client.post(
            "/api/v1/contacts/imports/paste/",
            {"rows": [{"name": "Bad", "phone": "12"}, {"name": "Good", "phone": "+919876543210"}]},
            format="json",
        )

        body = response.json()
        self.assertEqual(body["created"], 1)
        self.assertEqual(len(body["errors"]), 1)

    def test_a_repeat_within_one_request_is_imported_once(self):
        response = self.client.post(
            "/api/v1/contacts/imports/paste/",
            {"rows": [{"phone": "+919876543210"}, {"phone": "+919876543210"}]},
            format="json",
        )

        self.assertEqual(response.json()["created"], 1)
        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 1)

    def test_groups_and_tags_are_applied(self):
        from .models import ContactGroup, ContactTag

        group = ContactGroup.objects.create(tenant=self.tenant, name="VIP")
        tag = ContactTag.objects.create(tenant=self.tenant, name="paste")

        self.client.post(
            "/api/v1/contacts/imports/paste/",
            {
                "rows": [{"phone": "+919876543210"}],
                "group_ids": [group.pk],
                "tag_ids": [tag.pk],
            },
            format="json",
        )

        contact = Contact.objects.get(tenant=self.tenant, phone="+919876543210")
        self.assertEqual(list(contact.groups.values_list("name", flat=True)), ["VIP"])
        self.assertEqual(list(contact.tags.values_list("name", flat=True)), ["paste"])

    def test_a_group_from_another_workspace_is_ignored(self):
        from .models import ContactGroup

        other = Tenant.objects.create(name="Other", slug="paste-other-2", kind=Tenant.Kind.CUSTOMER)
        foreign = ContactGroup.objects.create(tenant=other, name="Theirs")

        self.client.post(
            "/api/v1/contacts/imports/paste/",
            {"rows": [{"phone": "+919876543210"}], "group_ids": [foreign.pk]},
            format="json",
        )

        contact = Contact.objects.get(tenant=self.tenant, phone="+919876543210")
        self.assertEqual(contact.groups.count(), 0)

    def test_the_import_returns_every_contact_it_resolved(self):
        """
        Not everybody pastes a list in order to *have* it. The campaign
        wizard pastes one in order to send to it, and an id list that
        covered only the rows this paste happened to create would build an
        audience missing every number already on the account.
        """
        existing = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+919876543210", name="Aman",
        )

        response = self.client.post(
            "/api/v1/contacts/imports/paste/",
            {"rows": [{"phone": "+919876543210"}, {"phone": "+919876543211"}]},
            format="json",
        )

        body = response.json()
        created = Contact.objects.get(tenant=self.tenant, phone="+919876543211")
        # In the order they were pasted, so an audience built from them is the
        # list the reviewer was looking at.
        self.assertEqual(body["contact_ids"], [existing.pk, created.pk])

    def test_an_unsubscribed_contact_is_returned_and_counted(self):
        """
        It stays in the id list -- the send path is the one authority on who
        is messaged, and it drops them there -- but the count says so, because
        a caller that thinks it just built an audience of two needs to know
        only one of them will be reached.
        """
        Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+919876543210", subscribed=False,
        )

        response = self.client.post(
            "/api/v1/contacts/imports/paste/",
            {"rows": [{"phone": "+919876543210"}, {"phone": "+919876543211"}]},
            format="json",
        )

        body = response.json()
        self.assertEqual(len(body["contact_ids"]), 2)
        self.assertEqual(body["unsubscribed"], 1)

    def test_a_row_the_server_refuses_is_absent_from_the_ids(self):
        response = self.client.post(
            "/api/v1/contacts/imports/paste/",
            {"rows": [{"phone": "12"}, {"phone": "+919876543210"}]},
            format="json",
        )

        body = response.json()
        self.assertEqual(
            body["contact_ids"],
            [Contact.objects.get(tenant=self.tenant, phone="+919876543210").pk],
        )

    def test_an_empty_import_is_refused(self):
        response = self.client.post(
            "/api/v1/contacts/imports/paste/", {"rows": []}, format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_both_endpoints_require_authentication(self):
        anonymous = APIClient()

        self.assertEqual(
            anonymous.post("/api/v1/contacts/imports/parse/", {"text": "1"}, format="json").status_code, 401,
        )
        self.assertEqual(
            anonymous.post("/api/v1/contacts/imports/paste/", {"rows": []}, format="json").status_code, 401,
        )
