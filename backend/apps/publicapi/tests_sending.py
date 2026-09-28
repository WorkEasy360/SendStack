"""
Tests for the send path, campaigns, templates, credits and idempotency.

The claim these are defending is the one the whole app rests on: a message sent
through the public API is the *same* message, on the same pipeline, under the
same rules, as one sent from the dashboard. So several of these assert on
internal state -- the Message row's `billed_to`, its `kind`, the credit
reservation, the Celery task -- rather than only on the HTTP response, because
the response would look identical if the façade had quietly grown its own send
path.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.campaigns.models import Campaign
from apps.contacts.models import Contact
from apps.messaging.models import Message
from apps.templates_app.models import MessageTemplate
from apps.whatsapp.models import WhatsAppAccount

from . import scopes
from .models import IdempotencyRecord
from .tests import API, make_account, make_key, make_tenant, make_user


@API
class SendMessageTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("send-tenant")
        self.user = make_user(self.tenant, "send-owner", credits=100)
        self.account = make_account(self.tenant, self.user)
        _, self.full = make_key(self.tenant, self.user)

    def send(self, payload, key=None, **extra):
        # `captureOnCommitCallbacks` because services.send_message publishes the
        # Celery task with `transaction.on_commit` -- correct in production (a
        # worker must never be handed a task for a row that has not committed
        # yet) and invisible inside TestCase's outer transaction without this.
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                "/api/public/v1/messages/send",
                data=payload,
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {key or self.full}",
                **extra,
            )

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_send_queues_a_message(self, mock_delay):
        response = self.send({
            "channel": "whatsapp",
            "account_id": f"account_{self.account.pk}",
            "to": "919999999999",
            "type": "text",
            "text": "Hello from my application",
        })

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["status"], "queued")
        self.assertTrue(body["message_id"].startswith("msg_"))

        message = Message.objects.get()
        self.assertEqual(message.tenant_id, self.tenant.pk)
        self.assertEqual(message.to_phone, "+919999999999")
        self.assertEqual(message.body, "Hello from my application")
        self.assertEqual(message.status, Message.Status.QUEUED)
        # The same Celery task the dashboard uses. If this ever stops being
        # called, the API has grown a send path of its own.
        mock_delay.assert_called_once_with(message.pk)

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_send_reserves_a_credit_against_the_key_owner(self, _delay):
        self.send({"to": "919999999999", "text": "Hi"})

        self.user.refresh_from_db()
        self.assertEqual(self.user.reserved_credits, 1)
        self.assertEqual(Message.objects.get().billed_to_id, self.user.pk)

    def test_send_without_credits_is_402(self):
        self.user.credits = 0
        self.user.save(update_fields=["credits"])

        response = self.send({"to": "919999999999", "text": "Hi"})

        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["error"]["code"], "INSUFFICIENT_CREDITS")
        self.assertFalse(Message.objects.exists())

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_send_creates_the_contact_so_compliance_applies(self, _delay):
        self.send({"to": "919999999999", "text": "Hi"})

        contact = Contact.objects.get(tenant=self.tenant, phone="+919999999999")
        # Without a contact the gate skips the opt-out check entirely -- see
        # apps.messaging.gating.evaluate.
        self.assertEqual(Message.objects.get().contact_id, contact.pk)

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_send_reuses_an_existing_contact(self, _delay):
        existing = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+919999999999", name="Aman",
        )

        self.send({"to": "919999999999", "text": "Hi", "name": "Someone Else"})

        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 1)
        existing.refresh_from_db()
        # Never overwritten -- the same rule the CSV importer follows.
        self.assertEqual(existing.name, "Aman")

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_send_defaults_to_marketing(self, _delay):
        self.send({"to": "919999999999", "text": "Hi"})

        self.assertEqual(Message.objects.get().kind, Message.Kind.MARKETING)

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_service_kind_is_accepted(self, _delay):
        self.send({"to": "919999999999", "text": "Hi", "kind": "service"})

        self.assertEqual(Message.objects.get().kind, Message.Kind.SERVICE)

    def test_unknown_kind_is_rejected(self):
        response = self.send({"to": "919999999999", "text": "Hi", "kind": "transactional"})

        self.assertEqual(response.status_code, 400)

    def test_a_refused_send_does_not_leak_a_credit_reservation(self):
        """
        Every refusal has to happen before the reservation. A request rejected
        for a bad field must not take a credit off the balance -- there would
        be no Message row for the send task to release it against, so the
        customer would simply be short one credit.
        """
        self.send({"to": "919999999999", "text": "Hi", "kind": "transactional"})
        self.send({"to": "not-a-number", "text": "Hi"})
        self.send({"to": "919999999999", "type": "image"})

        self.user.refresh_from_db()
        self.assertEqual(self.user.reserved_credits, 0)
        self.assertEqual(self.user.credits, 100)

    def test_invalid_phone_number(self):
        response = self.send({"to": "not-a-number", "text": "Hi"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_RECIPIENT")

    def test_empty_text_is_rejected(self):
        response = self.send({"to": "919999999999", "type": "text", "text": ""})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")

    def test_media_type_without_media_url_is_rejected(self):
        response = self.send({"to": "919999999999", "type": "image"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("media_url", response.json()["error"]["details"])

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_media_message(self, _delay):
        response = self.send({
            "to": "919999999999", "type": "image",
            "media_url": "https://example.com/x.png", "caption": "Receipt",
        })

        self.assertEqual(response.status_code, 202)
        message = Message.objects.get()
        self.assertEqual(message.message_type, Message.MessageType.IMAGE)
        self.assertEqual(message.media_url, "https://example.com/x.png")
        self.assertEqual(message.caption, "Receipt")

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_pdf_maps_to_document(self, _delay):
        self.send({"to": "919999999999", "type": "pdf", "media_url": "https://x/y.pdf"})

        self.assertEqual(Message.objects.get().message_type, Message.MessageType.DOCUMENT)

    def test_non_whatsapp_channel_is_refused(self):
        response = self.send({"channel": "sms", "to": "919999999999", "text": "Hi"})

        self.assertEqual(response.status_code, 400)

    def test_disconnected_account_is_409(self):
        WhatsAppAccount.objects.filter(pk=self.account.pk).update(
            status=WhatsAppAccount.Status.DISCONNECTED,
        )

        response = self.send({"to": "919999999999", "text": "Hi"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "ACCOUNT_DISCONNECTED")

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_account_is_chosen_when_none_is_named(self, _delay):
        response = self.send({"to": "919999999999", "text": "Hi"})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(Message.objects.get().whatsapp_account_id, self.account.pk)

    def test_test_key_cannot_send(self):
        from .models import ApiKey

        _, test_key = make_key(self.tenant, self.user, environment=ApiKey.Environment.TEST)

        response = self.send({"to": "919999999999", "text": "Hi"}, key=test_key)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "TEST_KEY_CANNOT_SEND")
        self.assertFalse(Message.objects.exists())

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_message_count_is_recorded_on_the_key(self, _delay):
        from .models import ApiKey

        self.send({"to": "919999999999", "text": "Hi"})

        self.assertEqual(ApiKey.objects.get(key_prefix__isnull=False).message_count, 1)


@API
class TemplateSendTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("tplsend-tenant")
        self.user = make_user(self.tenant, "tplsend-owner", credits=100)
        self.account = make_account(self.tenant, self.user)
        _, self.full = make_key(self.tenant, self.user)
        self.template = MessageTemplate.objects.create(
            tenant=self.tenant, created_by=self.user, name="welcome_customer",
            body="Hi [name], your order [order_id] is confirmed.",
            variables={"name": "there"},
        )

    def send(self, payload):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                "/api/public/v1/messages/send", data=payload,
                content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.full}",
            )

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_template_variables_are_substituted(self, _delay):
        response = self.send({
            "to": "919999999999",
            "template_id": "welcome_customer",
            "variables": {"name": "Aman", "order_id": "ORD123"},
        })

        self.assertEqual(response.status_code, 202)
        message = Message.objects.get()
        self.assertEqual(message.body, "Hi Aman, your order ORD123 is confirmed.")
        self.assertEqual(message.template_id, self.template.pk)

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_missing_variable_falls_back_to_the_templates_default(self, _delay):
        self.send({"to": "919999999999", "template_id": "welcome_customer", "variables": {"order_id": "X"}})

        self.assertEqual(Message.objects.get().body, "Hi there, your order X is confirmed.")

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_template_can_be_addressed_by_id(self, _delay):
        response = self.send({
            "to": "919999999999", "template_id": f"tpl_{self.template.pk}",
            "variables": {"name": "Aman", "order_id": "1"},
        })

        self.assertEqual(response.status_code, 202)

    def test_unknown_template_is_404(self):
        response = self.send({"to": "919999999999", "template_id": "nope"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "INVALID_TEMPLATE")

    def test_inactive_template_is_refused(self):
        MessageTemplate.objects.filter(pk=self.template.pk).update(is_active=False)

        response = self.send({"to": "919999999999", "template_id": "welcome_customer"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_TEMPLATE")

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_brace_syntax_also_renders(self, _delay):
        MessageTemplate.objects.filter(pk=self.template.pk).update(
            body="Hello {{name}}!",
        )

        self.send({"to": "919999999999", "template_id": "welcome_customer", "variables": {"name": "Priya"}})

        self.assertEqual(Message.objects.get().body, "Hello Priya!")


@API
class TemplateCrudTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("tplcrud-tenant")
        self.user = make_user(self.tenant, "tplcrud-owner")
        _, self.full = make_key(self.tenant, self.user)

    def auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.full}"}

    def test_create_list_update_delete(self):
        created = self.client.post(
            "/api/public/v1/templates",
            data={"name": "promo", "body": "Hi [name]"},
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(created.status_code, 201)
        template_id = created.json()["id"]
        self.assertEqual(created.json()["variables"], ["name"])

        listed = self.client.get("/api/public/v1/templates", **self.auth())
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["pagination"]["total"], 1)

        updated = self.client.put(
            f"/api/public/v1/templates/{template_id}",
            data={"body": "Hello [name]"},
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["body"], "Hello [name]")

        deleted = self.client.delete(f"/api/public/v1/templates/{template_id}", **self.auth())
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(MessageTemplate.objects.exists())

    def test_duplicate_name_is_409(self):
        MessageTemplate.objects.create(tenant=self.tenant, name="promo", body="x")

        response = self.client.post(
            "/api/public/v1/templates",
            data={"name": "promo", "body": "y"},
            content_type="application/json", **self.auth(),
        )

        self.assertEqual(response.status_code, 409)

    def test_template_used_by_a_campaign_cannot_be_deleted(self):
        account = make_account(self.tenant, self.user)
        template = MessageTemplate.objects.create(tenant=self.tenant, name="in_use", body="x")
        Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="C",
            whatsapp_account=account, template=template,
        )

        response = self.client.delete(f"/api/public/v1/templates/tpl_{template.pk}", **self.auth())

        self.assertEqual(response.status_code, 409)
        self.assertTrue(MessageTemplate.objects.filter(pk=template.pk).exists())


@API
class CampaignTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("camp-tenant")
        self.user = make_user(self.tenant, "camp-owner", credits=1000)
        self.account = make_account(self.tenant, self.user)
        _, self.full = make_key(self.tenant, self.user)

    def auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.full}"}

    def create(self, **overrides):
        payload = {
            "name": "January Promotion",
            "channel": "whatsapp",
            "account_id": f"account_{self.account.pk}",
            "text": "Hello [name]",
            "recipients": [
                {"phone": "919999999999", "variables": {"name": "Aman"}},
                {"phone": "918888888888", "variables": {"name": "Priya"}},
            ],
        }
        payload.update(overrides)
        return self.client.post(
            "/api/public/v1/campaigns", data=payload,
            content_type="application/json", **self.auth(),
        )

    def test_create_resolves_recipients_to_contacts(self):
        response = self.create()

        self.assertEqual(response.status_code, 201)
        campaign = Campaign.objects.get()
        # The existing audience type, not a new one -- so the dispatcher, the
        # compliance gate and the report all understand it unchanged.
        self.assertEqual(campaign.audience_type, Campaign.AudienceType.CONTACT_IDS)
        self.assertEqual(len(campaign.audience_contact_ids), 2)
        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 2)

    def test_per_recipient_variables_land_on_the_contact(self):
        self.create()

        contact = Contact.objects.get(phone="+919999999999")
        self.assertEqual(contact.custom_fields.get("name"), "Aman")

    def test_duplicate_recipients_are_collapsed(self):
        response = self.create(recipients=[
            {"phone": "919999999999"}, {"phone": "919999999999"},
        ])

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(Campaign.objects.get().audience_contact_ids), 1)

    def test_campaign_starts_in_draft(self):
        self.create()

        self.assertEqual(Campaign.objects.get().status, Campaign.Status.DRAFT)

    @patch("apps.campaigns.tasks.dispatch_campaign.delay")
    def test_start_launches_through_the_existing_service(self, mock_dispatch):
        campaign_id = self.create().json()["id"]

        response = self.client.post(
            f"/api/public/v1/campaigns/{campaign_id}/start", **self.auth(),
        )

        self.assertEqual(response.status_code, 200)
        campaign = Campaign.objects.get()
        self.assertEqual(campaign.status, Campaign.Status.QUEUED)
        self.assertEqual(campaign.total_recipients, 2)
        mock_dispatch.assert_called_once_with(campaign.pk)

    @patch("apps.campaigns.tasks.dispatch_campaign.delay")
    def test_inline_start(self, _dispatch):
        response = self.create(start=True)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Campaign.objects.get().status, Campaign.Status.QUEUED)

    @patch("apps.campaigns.tasks.dispatch_campaign.delay")
    def test_pause_resume_cancel(self, _dispatch):
        campaign_id = self.create(start=True).json()["id"]

        paused = self.client.post(f"/api/public/v1/campaigns/{campaign_id}/pause", **self.auth())
        self.assertEqual(paused.json()["status"], "paused")

        resumed = self.client.post(f"/api/public/v1/campaigns/{campaign_id}/resume", **self.auth())
        # RUNNING, not back to QUEUED: resuming re-enters dispatch rather than
        # re-queuing from scratch. See apps.campaigns.services.resume.
        self.assertEqual(resumed.json()["status"], "running")

        cancelled = self.client.post(f"/api/public/v1/campaigns/{campaign_id}/cancel", **self.auth())
        self.assertEqual(cancelled.json()["status"], "cancelled")

    def test_illegal_transition_is_409(self):
        campaign_id = self.create().json()["id"]

        response = self.client.post(f"/api/public/v1/campaigns/{campaign_id}/pause", **self.auth())

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "CAMPAIGN_STATE_INVALID")

    def test_cancel_needs_its_own_scope(self):
        _, limited = make_key(
            self.tenant, self.user,
            scope_list=[scopes.CAMPAIGNS_CREATE, scopes.CAMPAIGNS_READ, scopes.CAMPAIGNS_SEND],
        )
        campaign_id = self.create().json()["id"]

        response = self.client.post(
            f"/api/public/v1/campaigns/{campaign_id}/cancel",
            HTTP_AUTHORIZATION=f"Bearer {limited}",
        )

        self.assertEqual(response.status_code, 403)

    def test_empty_recipients_are_rejected(self):
        response = self.create(recipients=[])

        self.assertEqual(response.status_code, 400)

    @patch("apps.campaigns.tasks.dispatch_campaign.delay")
    def test_results_lists_per_recipient_messages(self, _dispatch):
        campaign_id = self.create(start=True).json()["id"]

        response = self.client.get(f"/api/public/v1/campaigns/{campaign_id}/results", **self.auth())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["data"]), 2)
        self.assertEqual(body["campaign"]["id"], campaign_id)


@API
class IdempotencyTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("idem-tenant")
        self.user = make_user(self.tenant, "idem-owner", credits=100)
        self.account = make_account(self.tenant, self.user)
        _, self.full = make_key(self.tenant, self.user)

    def send(self, payload, idem_key=None):
        extra = {"HTTP_AUTHORIZATION": f"Bearer {self.full}"}
        if idem_key:
            extra["HTTP_IDEMPOTENCY_KEY"] = idem_key
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                "/api/public/v1/messages/send", data=payload,
                content_type="application/json", **extra,
            )

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_retry_with_the_same_key_sends_once(self, mock_delay):
        payload = {"to": "919999999999", "text": "Hello"}

        first = self.send(payload, "order-123-message-1")
        second = self.send(payload, "order-123-message-1")

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        # The whole point: one message, one task, identical responses.
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(mock_delay.call_count, 1)
        self.assertEqual(first.json()["message_id"], second.json()["message_id"])
        self.assertEqual(second["Idempotency-Replayed"], "true")

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_replay_does_not_charge_a_second_credit(self, _delay):
        payload = {"to": "919999999999", "text": "Hello"}
        self.send(payload, "k1")
        self.send(payload, "k1")

        self.user.refresh_from_db()
        self.assertEqual(self.user.reserved_credits, 1)

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_same_key_different_body_is_a_conflict(self, _delay):
        self.send({"to": "919999999999", "text": "Hello"}, "k2")

        response = self.send({"to": "919999999999", "text": "Something else"}, "k2")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(Message.objects.count(), 1)

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_different_keys_send_twice(self, _delay):
        payload = {"to": "919999999999", "text": "Hello"}

        self.send(payload, "k3")
        self.send(payload, "k4")

        self.assertEqual(Message.objects.count(), 2)

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_no_key_means_no_protection(self, _delay):
        payload = {"to": "919999999999", "text": "Hello"}

        self.send(payload)
        self.send(payload)

        self.assertEqual(Message.objects.count(), 2)

    def test_a_failed_request_releases_the_key(self):
        # A validation failure sends nothing, so the key must be reusable --
        # otherwise a typo locks the caller out of their own key for a day.
        self.send({"to": "bad"}, "k5")

        self.assertFalse(IdempotencyRecord.objects.filter(key="k5").exists())

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_a_concurrent_retry_is_refused(self, _delay):
        from .models import IdempotencyRecord as Record

        # Simulates the first request still being in flight.
        Record.objects.create(
            tenant=self.tenant, key="k6", endpoint="messages/send",
            fingerprint=__import__("apps.publicapi.idempotency", fromlist=["x"]).fingerprint(
                {"to": "919999999999", "text": "Hello"}
            ),
            state=Record.State.IN_PROGRESS,
            expires_at=__import__("django.utils.timezone", fromlist=["x"]).now()
            + __import__("datetime", fromlist=["x"]).timedelta(hours=1),
        )

        response = self.send({"to": "919999999999", "text": "Hello"}, "k6")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "IDEMPOTENCY_IN_PROGRESS")
        self.assertFalse(Message.objects.exists())

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_idempotency_is_scoped_to_the_tenant(self, _delay):
        other_tenant = make_tenant("idem-other")
        other_user = make_user(other_tenant, "idem-other-owner", credits=100)
        make_account(other_tenant, other_user)
        _, other_key = make_key(other_tenant, other_user)

        payload = {"to": "919999999999", "text": "Hello"}
        self.send(payload, "shared-key")
        response = self.client.post(
            "/api/public/v1/messages/send", data=payload,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {other_key}",
            HTTP_IDEMPOTENCY_KEY="shared-key",
        )

        # One tenant's idempotency key must not suppress another's send.
        self.assertEqual(response.status_code, 202)
        self.assertEqual(Message.objects.count(), 2)


@API
@override_settings(PUBLIC_API_DEFAULT_REQUESTS_PER_MINUTE=1000)
class RateLimitTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.tenant = make_tenant("rl-tenant")
        self.user = make_user(self.tenant, "rl-owner", credits=1000)
        self.account = make_account(self.tenant, self.user)

    def test_request_ceiling_returns_429(self):
        _, full = make_key(self.tenant, self.user, requests_per_minute=3)

        codes = [
            self.client.get("/api/public/v1/ping", HTTP_AUTHORIZATION=f"Bearer {full}").status_code
            for _ in range(5)
        ]

        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3], 429)

    def test_429_carries_the_headers_a_client_needs(self):
        _, full = make_key(self.tenant, self.user, requests_per_minute=1)

        self.client.get("/api/public/v1/ping", HTTP_AUTHORIZATION=f"Bearer {full}")
        response = self.client.get("/api/public/v1/ping", HTTP_AUTHORIZATION=f"Bearer {full}")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "RATE_LIMITED")
        self.assertEqual(response["X-RateLimit-Limit"], "1")
        self.assertEqual(response["X-RateLimit-Remaining"], "0")
        self.assertIn("X-RateLimit-Reset", response)
        self.assertIn("Retry-After", response)

    def test_successful_responses_carry_the_headers_too(self):
        _, full = make_key(self.tenant, self.user, requests_per_minute=10)

        response = self.client.get("/api/public/v1/ping", HTTP_AUTHORIZATION=f"Bearer {full}")

        self.assertEqual(response["X-RateLimit-Limit"], "10")
        self.assertEqual(response["X-RateLimit-Remaining"], "9")

    @patch("apps.messaging.tasks.send_message_task.delay")
    def test_message_ceiling_is_separate_from_the_request_ceiling(self, _delay):
        _, full = make_key(
            self.tenant, self.user, requests_per_minute=100, messages_per_minute=2,
        )

        codes = []
        for _ in range(4):
            codes.append(self.client.post(
                "/api/public/v1/messages/send",
                data={"to": "919999999999", "text": "Hi"},
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {full}",
            ).status_code)

        self.assertEqual(codes[:2], [202, 202])
        self.assertEqual(codes[2], 429)
        # Refused before anything was written -- a 429 must not cost a credit.
        self.assertEqual(Message.objects.count(), 2)

    def test_the_bucket_that_stopped_you_is_named(self):
        _, full = make_key(self.tenant, self.user, requests_per_minute=100, messages_per_minute=1)

        with patch("apps.messaging.tasks.send_message_task.delay"):
            for _ in range(2):
                response = self.client.post(
                    "/api/public/v1/messages/send",
                    data={"to": "919999999999", "text": "Hi"},
                    content_type="application/json",
                    HTTP_AUTHORIZATION=f"Bearer {full}",
                )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["X-RateLimit-Bucket"], "messages_per_minute")

    def test_limits_are_per_key_not_per_tenant(self):
        _, first = make_key(self.tenant, self.user, requests_per_minute=1, name="one")
        _, second = make_key(self.tenant, self.user, requests_per_minute=1, name="two")

        self.client.get("/api/public/v1/ping", HTTP_AUTHORIZATION=f"Bearer {first}")
        exhausted = self.client.get("/api/public/v1/ping", HTTP_AUTHORIZATION=f"Bearer {first}")
        other = self.client.get("/api/public/v1/ping", HTTP_AUTHORIZATION=f"Bearer {second}")

        self.assertEqual(exhausted.status_code, 429)
        self.assertEqual(other.status_code, 200)


@API
class ContactAndAccountTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("ca-tenant")
        self.user = make_user(self.tenant, "ca-owner")
        self.account = make_account(self.tenant, self.user, name="Sales line")
        _, self.full = make_key(self.tenant, self.user)

    def auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.full}"}

    def test_account_list_shape(self):
        response = self.client.get("/api/public/v1/accounts", **self.auth())

        account = response.json()["accounts"][0]
        self.assertEqual(account["id"], f"account_{self.account.pk}")
        self.assertEqual(account["provider"], "cloud_api")
        self.assertEqual(account["status"], "connected")
        self.assertEqual(account["phone"], "+919162353602")

    def test_account_payload_leaks_no_credentials(self):
        response = self.client.get("/api/public/v1/accounts", **self.auth())

        serialised = response.content.decode()
        for forbidden in (
            "access_token", "app_secret", "webhook_verify_token",
            "provider_session_id", "phone_number_id", "token", "secret",
        ):
            self.assertNotIn(forbidden, serialised)

    def test_contact_upsert(self):
        created = self.client.post(
            "/api/public/v1/contacts",
            data={"phone": "919999999999", "name": "Aman", "email": "a@example.com"},
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["outcome"], "created")

        again = self.client.post(
            "/api/public/v1/contacts",
            data={"phone": "919999999999", "company": "Acme"},
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["outcome"], "updated")

        contact = Contact.objects.get()
        # Filled the gap, kept the existing value.
        self.assertEqual(contact.company, "Acme")
        self.assertEqual(contact.name, "Aman")

    def test_contact_reports_its_real_subscription_state(self):
        contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+919999999999", subscribed=False,
        )

        response = self.client.get(f"/api/public/v1/contacts/cnt_{contact.pk}", **self.auth())

        # An opted-out contact must not read as subscribed: an integration
        # deciding whether to message somebody reads this field.
        self.assertFalse(response.json()["subscribed"])

    def test_contact_search_normalises_the_phone(self):
        Contact.objects.create(tenant=self.tenant, created_by=self.user, phone="+919999999999")

        response = self.client.get(
            "/api/public/v1/contacts?phone=919999999999", **self.auth(),
        )

        self.assertEqual(response.json()["pagination"]["total"], 1)


@API
class MediaUploadTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("media-tenant")
        self.user = make_user(self.tenant, "media-owner")
        _, self.full = make_key(self.tenant, self.user)

    def upload(self, name, content, content_type="image/png"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return self.client.post(
            "/api/public/v1/media",
            data={"file": SimpleUploadedFile(name, content, content_type=content_type)},
            HTTP_AUTHORIZATION=f"Bearer {self.full}",
        )

    def test_upload_returns_an_absolute_url(self):
        # A real PNG signature, byte by byte: `core.uploads.validate_upload`
        # derives the type from the bytes, not from the name or the declared
        # content type. Written as integers rather than escape sequences so
        # nothing between here and the file can reinterpret them.
        png = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + bytes(64)

        response = self.upload("photo.png", png)

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["url"].startswith("http"))
        self.assertEqual(body["kind"], "IMAGE")

    def test_a_disguised_file_is_refused(self):
        # A PHP script named .png. The bytes decide, so this is refused --
        # which is the whole point of routing through validate_upload rather
        # than trusting the upload's own name.
        response = self.upload("evil.png", b"<?php system($_GET['c']); ?>")

        self.assertEqual(response.status_code, 400)

    def test_missing_file_is_a_clear_error(self):
        response = self.client.post(
            "/api/public/v1/media", data={}, HTTP_AUTHORIZATION=f"Bearer {self.full}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")

    def test_upload_needs_the_scope(self):
        _, limited = make_key(self.tenant, self.user, scope_list=[scopes.MESSAGES_SEND])
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.client.post(
            "/api/public/v1/media",
            data={"file": SimpleUploadedFile(
                "a.png",
                bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + bytes(64), content_type="image/png")},
            HTTP_AUTHORIZATION=f"Bearer {limited}",
        )

        self.assertEqual(response.status_code, 403)


@API
class QueryParameterTests(TestCase):
    """
    Bad query parameters must be empty results or 400s, never 500s.

    Each of these used to reach an integer field lookup with a string and
    raise ValueError, which the error handler renders as an opaque 500.
    """

    def setUp(self):
        self.tenant = make_tenant("qp-tenant")
        self.user = make_user(self.tenant, "qp-owner")
        make_account(self.tenant, self.user)
        _, self.full = make_key(self.tenant, self.user)

    def auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.full}"}

    def test_non_numeric_webhook_endpoint_filter(self):
        response = self.client.get(
            "/api/public/v1/webhooks/deliveries?endpoint_id=not-a-number", **self.auth(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], [])

    def test_non_numeric_account_filter_on_messages(self):
        response = self.client.get(
            "/api/public/v1/messages?account_id=not-a-number", **self.auth(),
        )

        self.assertEqual(response.status_code, 200)

    def test_unknown_message_status_is_a_400_not_a_500(self):
        response = self.client.get("/api/public/v1/messages?status=exploded", **self.auth())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")

    def test_nonsense_pagination_is_clamped(self):
        response = self.client.get(
            "/api/public/v1/messages?limit=abc&offset=-99", **self.auth(),
        )

        self.assertEqual(response.status_code, 200)
        pagination = response.json()["pagination"]
        self.assertEqual(pagination["offset"], 0)
        self.assertLessEqual(pagination["limit"], 200)

    def test_limit_is_capped(self):
        response = self.client.get("/api/public/v1/messages?limit=100000", **self.auth())

        self.assertEqual(response.json()["pagination"]["limit"], 200)

    def test_garbage_message_id_is_a_404(self):
        response = self.client.get("/api/public/v1/messages/msg_notanumber", **self.auth())

        self.assertEqual(response.status_code, 404)
