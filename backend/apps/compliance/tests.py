from datetime import time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.compliance import keywords
from apps.compliance.models import CompliancePolicy, ConsentEvent
from apps.compliance.services import apply_opt_out_affordance, handle_inbound, quiet_hours_delay
from apps.contacts.models import Contact
from apps.inbox.models import Conversation
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount


def _tenant(slug):
    return Tenant.objects.create(name=slug, slug=slug, kind=Tenant.Kind.CUSTOMER)


def _account(tenant, user, **overrides):
    account = WhatsAppAccount(
        tenant=tenant, user=user, name="Support Line",
        account_type=WhatsAppAccount.AccountType.API,
        connection_method=WhatsAppAccount.ConnectionMethod.CLOUD_API,
        phone_number_id="1234567890", status=WhatsAppAccount.Status.CONNECTED,
        **overrides,
    )
    account.set_access_token("token")
    account.save()
    return account


class StopKeywordMatchingTests(TestCase):
    """
    The matcher has to fire on what people actually type and stay silent on
    everything else. The second half is the hard one: a false positive here
    silently deletes a paying customer's audience one contact at a time.
    """

    def test_the_word_on_its_own_opts_out(self):
        for text in ["STOP", "stop", " Stop. ", "unsubscribe", "opt out", "OPT-OUT"]:
            self.assertTrue(keywords.is_stop(text), text)

    def test_a_short_sentence_containing_it_opts_out(self):
        for text in ["please stop", "stop sending", "pls unsubscribe me"]:
            self.assertTrue(keywords.is_stop(text), text)

    def test_a_long_sentence_merely_mentioning_it_does_not(self):
        for text in [
            "I will never stop recommending you to my friends",
            "we had to stop the delivery at the warehouse yesterday",
        ]:
            self.assertFalse(keywords.is_stop(text), text)

    def test_a_keyword_inside_a_longer_word_does_not_match(self):
        # "stopover" and "trabajando" contain a keyword as a substring; word
        # boundaries are the only thing keeping them out.
        for text in ["stopover", "bajando", "unsubscribed already thanks"]:
            self.assertFalse(keywords.is_stop(text), text)

    def test_other_languages_are_recognised(self):
        for text in ["band karo", "बंद करो", "berhenti", "arrêter", "إيقاف", "стоп"]:
            self.assertTrue(keywords.is_stop(text), text)

    def test_start_words_are_separate_from_stop_words(self):
        self.assertTrue(keywords.is_start("start"))
        self.assertFalse(keywords.is_stop("start"))

    def test_a_tenant_can_add_its_own_phrase(self):
        self.assertFalse(keywords.is_stop("cancelar"))
        self.assertTrue(keywords.is_stop("cancelar", extra=["cancelar"]))

    def test_an_empty_message_matches_nothing(self):
        self.assertFalse(keywords.is_stop(""))
        self.assertFalse(keywords.is_start("   "))


class InboundConsentTests(TestCase):
    def setUp(self):
        self.tenant = _tenant("acme-consent")
        self.user = get_user_model().objects.create_user(
            username="acme-consent-owner", password="x", tenant=self.tenant,
        )
        self.account = _account(self.tenant, self.user)
        self.contact = Contact.objects.create(
            tenant=self.tenant, phone="+15551230000", created_by=self.user, name="Sam",
        )
        self.conversation = Conversation.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
        )

    def _inbound(self, body="", button_id=""):
        return Message.objects.create(
            tenant=self.tenant, conversation=self.conversation, whatsapp_account=self.account,
            contact=self.contact, to_phone=self.contact.phone, direction=Message.Direction.INBOUND,
            body=body, replied_button_id=button_id, status=Message.Status.DELIVERED,
        )

    @patch("apps.automation.services.send_automated_message")
    def test_stop_unsubscribes_and_records_why(self, _send):
        handled = handle_inbound(self._inbound("STOP"))

        self.contact.refresh_from_db()
        self.assertTrue(handled)
        self.assertFalse(self.contact.subscribed)
        self.assertIsNotNone(self.contact.unsubscribed_at)

        event = ConsentEvent.objects.get(contact=self.contact)
        self.assertEqual(event.event, ConsentEvent.Event.OPT_OUT)
        self.assertEqual(event.source, ConsentEvent.Source.INBOUND_KEYWORD)
        self.assertIn("STOP", event.detail)

    @patch("apps.automation.services.send_automated_message")
    def test_tapping_the_unsubscribe_button_opts_out(self, _send):
        handled = handle_inbound(self._inbound("Unsubscribe", button_id=keywords.OPT_OUT_BUTTON_ID))

        self.contact.refresh_from_db()
        self.assertTrue(handled)
        self.assertFalse(self.contact.subscribed)
        self.assertEqual(
            ConsentEvent.objects.get(contact=self.contact).source, ConsentEvent.Source.INBOUND_BUTTON,
        )

    @patch("apps.automation.services.send_automated_message")
    def test_the_button_is_honoured_even_with_keyword_detection_off(self, _send):
        CompliancePolicy.objects.create(tenant=self.tenant, stop_keywords_enabled=False)

        self.assertTrue(handle_inbound(self._inbound("Unsubscribe", button_id=keywords.OPT_OUT_BUTTON_ID)))
        self.contact.refresh_from_db()
        self.assertFalse(self.contact.subscribed)

    def test_keyword_detection_can_be_switched_off(self):
        CompliancePolicy.objects.create(tenant=self.tenant, stop_keywords_enabled=False)

        self.assertFalse(handle_inbound(self._inbound("STOP")))
        self.contact.refresh_from_db()
        self.assertTrue(self.contact.subscribed)

    @patch("apps.automation.services.send_automated_message")
    def test_start_resubscribes_someone_who_opted_out(self, _send):
        self.contact.subscribed = False
        self.contact.save(update_fields=["subscribed"])

        self.assertTrue(handle_inbound(self._inbound("START")))
        self.contact.refresh_from_db()
        self.assertTrue(self.contact.subscribed)
        self.assertTrue(ConsentEvent.objects.filter(contact=self.contact, event=ConsentEvent.Event.OPT_IN).exists())

    def test_start_from_a_subscribed_contact_is_left_to_the_keyword_rules(self):
        # Otherwise a rule keyed on "start" could never fire: compliance would
        # swallow the message before automation ever saw it.
        self.assertFalse(handle_inbound(self._inbound("start")))

    @patch("apps.automation.services.send_automated_message")
    def test_the_confirmation_is_exempt_from_the_rules_it_confirms(self, send):
        handle_inbound(self._inbound("stop"))

        self.assertTrue(send.called)
        self.assertTrue(send.call_args.kwargs["policy_exempt"])

    @patch("apps.automation.services.send_automated_message")
    def test_no_confirmation_is_sent_when_it_is_switched_off(self, send):
        CompliancePolicy.objects.create(tenant=self.tenant, send_opt_out_confirmation=False)

        handle_inbound(self._inbound("stop"))
        self.assertFalse(send.called)

    @patch("apps.automation.services.send_automated_message")
    def test_opting_out_twice_records_both_requests_but_changes_nothing(self, _send):
        handle_inbound(self._inbound("stop"))
        handle_inbound(self._inbound("stop"))

        self.assertEqual(ConsentEvent.objects.filter(contact=self.contact).count(), 2)
        self.contact.refresh_from_db()
        self.assertFalse(self.contact.subscribed)


class AutomationStandsDownForOptOutTests(TestCase):
    """
    The rule that matters most: nobody who just asked to be left alone gets
    marketing back. Exercised through the real signal, because the ordering
    inside the receiver is the thing being tested.
    """

    def setUp(self):
        self.tenant = _tenant("acme-standdown")
        self.user = get_user_model().objects.create_user(
            username="acme-standdown-owner", password="x", tenant=self.tenant,
        )
        self.account = _account(self.tenant, self.user)
        self.contact = Contact.objects.create(
            tenant=self.tenant, phone="+15551230001", created_by=self.user,
        )
        self.conversation = Conversation.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
        )

    @patch("apps.automation.services.trigger_keyword_rules")
    @patch("apps.automation.services.trigger_welcome_message")
    @patch("apps.compliance.services._send_confirmation")
    def test_no_welcome_or_keyword_reply_follows_a_stop(self, _confirm, welcome, keyword_rules):
        from apps.automation.receivers import evaluate_automation_rules

        message = Message.objects.create(
            tenant=self.tenant, conversation=self.conversation, whatsapp_account=self.account,
            contact=self.contact, to_phone=self.contact.phone, direction=Message.Direction.INBOUND,
            body="stop", status=Message.Status.DELIVERED,
        )
        evaluate_automation_rules(sender=None, message=message, is_new_conversation=True)

        self.assertFalse(welcome.called)
        self.assertFalse(keyword_rules.called)


class QuietHoursTests(TestCase):
    def setUp(self):
        self.tenant = _tenant("acme-quiet")
        self.user = get_user_model().objects.create_user(
            username="acme-quiet-owner", password="x", tenant=self.tenant,
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, phone="+15551230002", created_by=self.user, country="IN",
        )
        self.policy = CompliancePolicy(
            tenant=self.tenant, quiet_hours_enabled=True,
            quiet_hours_start=time(21, 0), quiet_hours_end=time(9, 0), timezone="UTC",
        )

    def _at(self, hour, minute=0):
        return timezone.now().replace(hour=hour, minute=minute, second=0, microsecond=0)

    def test_a_message_inside_the_window_is_held_until_it_opens(self):
        delay = quiet_hours_delay(self.policy, self.contact, now=self._at(23))
        self.assertAlmostEqual(delay, 10 * 3600, delta=60)

    def test_a_message_in_the_early_hours_waits_only_until_morning(self):
        delay = quiet_hours_delay(self.policy, self.contact, now=self._at(3))
        self.assertAlmostEqual(delay, 6 * 3600, delta=60)

    def test_a_message_during_the_day_goes_out_now(self):
        self.assertEqual(quiet_hours_delay(self.policy, self.contact, now=self._at(14)), 0.0)

    def test_a_window_that_does_not_wrap_midnight_still_works(self):
        self.policy.quiet_hours_start = time(1, 0)
        self.policy.quiet_hours_end = time(6, 0)

        self.assertGreater(quiet_hours_delay(self.policy, self.contact, now=self._at(3)), 0)
        self.assertEqual(quiet_hours_delay(self.policy, self.contact, now=self._at(23)), 0.0)

    def test_identical_start_and_end_is_no_window_rather_than_a_permanent_one(self):
        self.policy.quiet_hours_start = time(9, 0)
        self.policy.quiet_hours_end = time(9, 0)

        self.assertEqual(quiet_hours_delay(self.policy, self.contact, now=self._at(23)), 0.0)

    def test_disabled_quiet_hours_never_hold_anything(self):
        self.policy.quiet_hours_enabled = False
        self.assertEqual(quiet_hours_delay(self.policy, self.contact, now=self._at(23)), 0.0)

    def test_the_contacts_own_timezone_is_used_when_asked_for(self):
        # 20:00 UTC is 01:30 the next day in Kolkata, which is inside the
        # window even though it is outside it in the tenant's own timezone.
        self.policy.use_contact_timezone = True

        self.assertGreater(quiet_hours_delay(self.policy, self.contact, now=self._at(20)), 0)
        self.policy.use_contact_timezone = False
        self.assertEqual(quiet_hours_delay(self.policy, self.contact, now=self._at(20)), 0.0)

    def test_an_unknown_country_falls_back_to_the_tenant_timezone(self):
        self.policy.use_contact_timezone = True
        self.contact.country = "ZZ"

        self.assertEqual(quiet_hours_delay(self.policy, self.contact, now=self._at(14)), 0.0)

    def test_a_hold_is_never_longer_than_a_day(self):
        delay = quiet_hours_delay(self.policy, self.contact, now=self._at(21, 1))
        self.assertLessEqual(delay, 24 * 3600)


class OptOutAffordanceTests(TestCase):
    """
    The footer is the default because it renders everywhere; the button is
    what a tenant opts into. Both have to end up on the message -- the one
    thing that must never happen is a broadcast going out with neither.
    """

    def setUp(self):
        self.policy = CompliancePolicy(
            tenant_id=None, opt_out_style=CompliancePolicy.OptOutStyle.BUTTON,
        )

    def test_a_reply_button_is_added_when_the_connection_supports_one(self):
        result = apply_opt_out_affordance(
            self.policy, body="New arrivals are in", caption="", buttons=[], supports_buttons=True,
        )

        self.assertEqual(len(result["buttons"]), 1)
        self.assertEqual(result["buttons"][0]["id"], keywords.OPT_OUT_BUTTON_ID)
        self.assertEqual(result["body"], "New arrivals are in")

    def test_it_joins_buttons_that_are_already_there(self):
        result = apply_opt_out_affordance(
            self.policy, body="Coming?", caption="",
            buttons=[{"type": "REPLY", "id": "yes", "title": "Yes", "url": ""}],
            supports_buttons=True,
        )

        self.assertEqual([button["id"] for button in result["buttons"]], ["yes", keywords.OPT_OUT_BUTTON_ID])

    def test_a_full_set_of_buttons_falls_back_to_the_footer(self):
        buttons = [
            {"type": "REPLY", "id": f"b{index}", "title": f"B{index}", "url": ""} for index in range(3)
        ]
        result = apply_opt_out_affordance(
            self.policy, body="Pick one", caption="", buttons=buttons, supports_buttons=True,
        )

        self.assertEqual(len(result["buttons"]), 3)
        self.assertIn("STOP", result["body"])

    def test_a_link_button_falls_back_to_the_footer(self):
        # A URL button and a reply button cannot travel in the same message,
        # so adding one would make the message unsendable.
        result = apply_opt_out_affordance(
            self.policy, body="Shop now", caption="",
            buttons=[{"type": "URL", "id": "", "title": "Shop", "url": "https://example.test"}],
            supports_buttons=True,
        )

        self.assertEqual(len(result["buttons"]), 1)
        self.assertIn("STOP", result["body"])

    def test_a_connection_without_buttons_falls_back_to_the_footer(self):
        result = apply_opt_out_affordance(
            self.policy, body="Hello", caption="", buttons=[], supports_buttons=False,
        )

        self.assertEqual(result["buttons"], [])
        self.assertIn("STOP", result["body"])

    def test_a_media_only_message_gets_the_footer_on_its_caption(self):
        result = apply_opt_out_affordance(
            self.policy, body="", caption="Our new catalogue", buttons=[], supports_buttons=True,
        )

        self.assertIn("STOP", result["caption"])
        self.assertEqual(result["body"], "")

    def test_it_is_never_applied_twice(self):
        once = apply_opt_out_affordance(
            self.policy, body="Hello", caption="", buttons=[], supports_buttons=False,
        )
        twice = apply_opt_out_affordance(self.policy, supports_buttons=False, **once)

        self.assertEqual(once["body"], twice["body"])

    def test_the_footer_is_what_a_tenant_gets_without_asking(self):
        # Defaulting to a button would silently turn every campaign into an
        # interactive message, which a QR connection cannot promise to render.
        result = apply_opt_out_affordance(
            CompliancePolicy(tenant_id=None), body="Hello", caption="", buttons=[], supports_buttons=True,
        )

        self.assertEqual(result["buttons"], [])
        self.assertIn("STOP", result["body"])

    def test_an_existing_unsubscribe_button_is_left_alone(self):
        buttons = [{"type": "REPLY", "id": keywords.OPT_OUT_BUTTON_ID, "title": "No thanks", "url": ""}]
        result = apply_opt_out_affordance(
            self.policy, body="Hi", caption="", buttons=buttons, supports_buttons=True,
        )

        self.assertEqual(result["buttons"], buttons)
        self.assertEqual(result["body"], "Hi")


class ConsentApiTests(TestCase):
    def setUp(self):
        self.tenant = _tenant("acme-consent-api")
        self.user = get_user_model().objects.create_user(
            username="acme-consent-api-owner", password="x", tenant=self.tenant,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_the_policy_is_created_on_first_read(self):
        response = self.client.get("/api/v1/compliance/policy/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["stop_keywords_enabled"])
        self.assertTrue(CompliancePolicy.objects.filter(tenant=self.tenant).exists())

    def test_quiet_hours_need_two_different_times(self):
        response = self.client.patch(
            "/api/v1/compliance/policy/",
            data={"quiet_hours_enabled": True, "quiet_hours_start": "09:00", "quiet_hours_end": "09:00"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("quiet_hours_end", response.json())

    def test_an_unknown_timezone_is_rejected(self):
        response = self.client.patch(
            "/api/v1/compliance/policy/", data={"timezone": "Mars/Olympus"}, content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    def test_the_keyword_preview_reports_what_the_server_would_do(self):
        response = self.client.post(
            "/api/v1/compliance/keyword-preview/", data={"text": "please stop"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["stops"])

    def test_contact_unsubscribe_records_who_did_it(self):
        contact = Contact.objects.create(tenant=self.tenant, phone="+15551230003", created_by=self.user)

        response = self.client.post(f"/api/v1/contacts/contacts/{contact.pk}/unsubscribe/")

        self.assertEqual(response.status_code, 200)
        event = ConsentEvent.objects.get(contact=contact)
        self.assertEqual(event.source, ConsentEvent.Source.AGENT)
        self.assertEqual(event.actor, self.user)

    def test_consent_history_is_readable_per_contact(self):
        contact = Contact.objects.create(tenant=self.tenant, phone="+15551230004", created_by=self.user)
        self.client.post(f"/api/v1/contacts/contacts/{contact.pk}/unsubscribe/")
        self.client.post(f"/api/v1/contacts/contacts/{contact.pk}/resubscribe/")

        response = self.client.get(f"/api/v1/contacts/contacts/{contact.pk}/consent-history/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sorted(row["event"] for row in response.json()), ["OPT_IN", "OPT_OUT"],
        )

    def test_another_tenants_consent_events_are_not_listed(self):
        other = _tenant("other-consent-api")
        other_user = get_user_model().objects.create_user(
            username="other-consent-api-owner", password="x", tenant=other,
        )
        other_contact = Contact.objects.create(tenant=other, phone="+15551230005", created_by=other_user)
        ConsentEvent.objects.create(
            tenant=other, contact=other_contact, event=ConsentEvent.Event.OPT_OUT,
            source=ConsentEvent.Source.API,
        )

        response = self.client.get("/api/v1/compliance/consent-events/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)


class ConsentEventRetentionTests(TestCase):
    def test_deleting_the_message_keeps_the_consent_record(self):
        tenant = _tenant("acme-retention")
        user = get_user_model().objects.create_user(
            username="acme-retention-owner", password="x", tenant=tenant,
        )
        account = _account(tenant, user)
        contact = Contact.objects.create(tenant=tenant, phone="+15551230006", created_by=user)
        message = Message.objects.create(
            tenant=tenant, whatsapp_account=account, contact=contact, to_phone=contact.phone,
            direction=Message.Direction.INBOUND, body="stop",
        )
        event = ConsentEvent.objects.create(
            tenant=tenant, contact=contact, event=ConsentEvent.Event.OPT_OUT,
            source=ConsentEvent.Source.INBOUND_KEYWORD, message=message,
        )

        message.delete()
        event.refresh_from_db()

        self.assertIsNone(event.message_id)
        self.assertEqual(event.event, ConsentEvent.Event.OPT_OUT)


class PolicyDefaultsTests(TestCase):
    def test_a_tenant_with_no_row_still_gets_a_working_policy(self):
        tenant = _tenant("acme-defaults")

        policy = CompliancePolicy.for_tenant(tenant)

        self.assertIsNone(policy.pk)
        self.assertTrue(policy.stop_keywords_enabled)
        self.assertFalse(policy.quiet_hours_enabled)
        self.assertEqual(CompliancePolicy.objects.count(), 0)

    def test_an_expired_hold_window_is_bounded(self):
        # Guards the constant itself: a policy that somehow produced a delay
        # of days would silently park a whole campaign.
        from apps.compliance.services import MAX_QUIET_HOLD_SECONDS

        self.assertLessEqual(MAX_QUIET_HOLD_SECONDS, timedelta(days=1).total_seconds())
