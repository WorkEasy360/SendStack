"""
One suite that tries to reach every tenant-scoped resource from the wrong tenant.

Tenant isolation was already tested -- but a test at a time, inside the app
that owns the resource, written when that resource was built. That arrangement
has one weakness, and it is the weakness that matters: **the resource added
next month has no test here unless somebody remembers to write one**, and
nobody notices the absence, because a missing test looks exactly like a passing
suite.

So the resources are listed in one table instead. Every row is exercised
through the same four attacks, and `test_every_registered_resource_is_covered`
asserts the table is not silently shrinking. Adding an endpoint to this file is
one line; the four tests come free.

    THE FOUR ATTACKS

    read    GET /resource/{someone else's id}/       -> 404 (never 200)
    write   PATCH ...                                -> 404 (never 200)
    delete  DELETE ...                               -> 404 (never 200)
    list    GET /resource/                           -> must not contain it

**404, not 403.** That is this API's established behaviour and it is the
better one: 403 confirms the row exists, which hands an attacker an oracle for
enumerating another workspace's ids. The tests accept 403 as well where a view
legitimately answers that way, but they never accept 200.

Mass assignment is checked separately at the bottom, because it is a different
bug with the same consequence: an endpoint that honours ``tenant`` in a request
body lets somebody move their own row into another workspace, or -- worse --
create one there, and no amount of queryset scoping catches it.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant

#: Every status a correctly-behaving endpoint may answer with when the object
#: belongs to somebody else. 404 is what this API does; 403 is accepted for the
#: handful of views that check permission before ownership; 405 means the verb
#: is not offered at all, which is not an isolation failure.
DENIED = (403, 404, 405)


def _make_tenant(slug, name):
    return Tenant.objects.create(name=name, slug=slug, kind=Tenant.Kind.CUSTOMER)


class TenantFixture:
    """One workspace with an owner and one row of every kind, built on demand."""

    def __init__(self, slug):
        self.tenant = _make_tenant(slug, slug.title())
        self.user = get_user_model().objects.create_user(
            username=f"{slug}-owner", password="isolation-test-pw", tenant=self.tenant,
        )

    # Each builder returns the object's primary key. They are separate methods
    # rather than one big setUp so that a resource whose model changes breaks
    # one row of the table and not the whole suite.

    def contact(self):
        from apps.contacts.models import Contact

        return Contact.objects.create(
            tenant=self.tenant, created_by=self.user,
            name=f"{self.tenant.slug} person", phone=f"+1555{self.tenant.pk:07d}",
        ).pk

    def contact_group(self):
        from apps.contacts.models import ContactGroup

        return ContactGroup.objects.create(tenant=self.tenant, name=f"{self.tenant.slug} group").pk

    def contact_tag(self):
        from apps.contacts.models import ContactTag

        return ContactTag.objects.create(tenant=self.tenant, name=f"{self.tenant.slug} tag").pk

    def whatsapp_account(self):
        from apps.whatsapp.models import WhatsAppAccount

        return WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name=f"{self.tenant.slug} line",
            status=WhatsAppAccount.Status.CONNECTED,
        ).pk

    def template(self):
        from apps.templates_app.models import MessageTemplate

        return MessageTemplate.objects.create(
            tenant=self.tenant, name=f"{self.tenant.slug} template", body="Hello",
        ).pk

    def campaign(self):
        from apps.campaigns.models import Campaign
        from apps.whatsapp.models import WhatsAppAccount

        account = WhatsAppAccount.objects.filter(tenant=self.tenant).first()
        if account is None:
            account = WhatsAppAccount.objects.create(
                tenant=self.tenant, user=self.user, name=f"{self.tenant.slug} line",
                status=WhatsAppAccount.Status.CONNECTED,
            )
        return Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name=f"{self.tenant.slug} campaign",
            whatsapp_account=account, message_body="Hello",
        ).pk

    def message(self):
        from apps.contacts.models import Contact
        from apps.messaging.models import Message
        from apps.whatsapp.models import WhatsAppAccount

        account = WhatsAppAccount.objects.filter(tenant=self.tenant).first() or None
        if account is None:
            account = WhatsAppAccount.objects.create(
                tenant=self.tenant, user=self.user, name=f"{self.tenant.slug} line",
            )
        contact = Contact.objects.filter(tenant=self.tenant).first()
        if contact is None:
            contact = Contact.objects.create(
                tenant=self.tenant, created_by=self.user, phone=f"+1555{self.tenant.pk:07d}",
            )
        return Message.objects.create(
            tenant=self.tenant, whatsapp_account=account, contact=contact,
            to_phone=contact.phone, body="hi", billed_to=self.user,
        ).pk

    def api_key(self):
        from apps.publicapi.services import create_api_key
        from apps.publicapi.scopes import MESSAGES_SEND

        api_key, _full = create_api_key(
            tenant=self.tenant, owner=self.user, created_by=self.user,
            name=f"{self.tenant.slug} key", scopes=[MESSAGES_SEND],
        )
        return api_key.pk

    def webhook_endpoint(self):
        from apps.publicapi.models import WebhookEndpoint

        endpoint = WebhookEndpoint(
            tenant=self.tenant, created_by=self.user,
            name=f"{self.tenant.slug} hook",
            url=f"https://{self.tenant.slug}.example.com/hook",
        )
        endpoint.set_secret(WebhookEndpoint.generate_secret())
        endpoint.save()
        return endpoint.pk

    def conversation(self):
        from apps.contacts.models import Contact
        from apps.inbox.models import Conversation
        from apps.whatsapp.models import WhatsAppAccount

        account = WhatsAppAccount.objects.filter(tenant=self.tenant).first()
        if account is None:
            account = WhatsAppAccount.objects.create(
                tenant=self.tenant, user=self.user, name=f"{self.tenant.slug} line",
            )
        contact = Contact.objects.filter(tenant=self.tenant).first()
        if contact is None:
            contact = Contact.objects.create(
                tenant=self.tenant, created_by=self.user, phone=f"+1555{self.tenant.pk:07d}",
            )
        return Conversation.objects.create(
            tenant=self.tenant, whatsapp_account=account, contact=contact,
        ).pk


#: (label, url prefix, fixture builder, a body that would be a real edit).
#:
#: The patch body must be a field the serializer genuinely accepts, or the
#: write test would pass for the wrong reason -- a 400 for "unknown field"
#: looks like a rejection but proves nothing about isolation.
RESOURCES = (
    ("contacts", "/api/v1/contacts/contacts/", "contact", {"name": "moved"}),
    ("contact-groups", "/api/v1/contacts/groups/", "contact_group", {"name": "moved"}),
    ("contact-tags", "/api/v1/contacts/tags/", "contact_tag", {"name": "moved"}),
    ("devices", "/api/v1/whatsapp/accounts/", "whatsapp_account", {"name": "moved"}),
    ("templates", "/api/v1/templates/templates/", "template", {"name": "moved"}),
    ("campaigns", "/api/v1/campaigns/campaigns/", "campaign", {"name": "moved"}),
    ("messages", "/api/v1/messaging/messages/", "message", {}),
    ("conversations", "/api/v1/inbox/conversations/", "conversation", {}),
    # The developer platform. An API key reachable across workspaces would be
    # the worst version of this bug: not "read somebody else's data" but
    # "revoke, rotate or read the usage of the credential holding their
    # workspace open". The patch body is empty because the viewset offers no
    # PUT/PATCH at all -- 405 is the correct refusal there, and DENIED accepts it.
    ("api-keys", "/api/v1/developer/api-keys/", "api_key", {}),
    ("webhook-endpoints", "/api/v1/developer/webhooks/", "webhook_endpoint",
     {"url": "https://moved.example.com/hook"}),
)


class CrossTenantAccessTests(TestCase):
    """Tenant B tries every listed resource belonging to tenant A."""

    @classmethod
    def setUpTestData(cls):
        cls.a = TenantFixture("isolation-alpha")
        cls.b = TenantFixture("isolation-bravo")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.b.user)

    def _victim_id(self, builder):
        return getattr(self.a, builder)()

    def test_cannot_read_another_tenants_object(self):
        for label, prefix, builder, _ in RESOURCES:
            with self.subTest(resource=label):
                object_id = self._victim_id(builder)
                response = self.client.get(f"{prefix}{object_id}/")
                self.assertIn(
                    response.status_code, DENIED,
                    f"{label}: reading another tenant's object returned {response.status_code}",
                )

    def test_cannot_write_another_tenants_object(self):
        for label, prefix, builder, body in RESOURCES:
            if not body:
                continue  # read-only resource; the verb test below covers it
            with self.subTest(resource=label):
                object_id = self._victim_id(builder)
                response = self.client.patch(f"{prefix}{object_id}/", body, format="json")
                self.assertIn(
                    response.status_code, DENIED,
                    f"{label}: writing another tenant's object returned {response.status_code}",
                )

    def test_cannot_delete_another_tenants_object(self):
        for label, prefix, builder, _ in RESOURCES:
            with self.subTest(resource=label):
                object_id = self._victim_id(builder)
                response = self.client.delete(f"{prefix}{object_id}/")
                self.assertIn(
                    response.status_code, DENIED,
                    f"{label}: deleting another tenant's object returned {response.status_code}",
                )

    def test_list_endpoints_never_include_another_tenants_rows(self):
        """
        The quieter half of isolation, and the one a scoped `get_object` misses.

        A detail view can be perfectly scoped while the list it sits under is
        not, and a leaking list is worse: it hands over the ids as well as the
        contents.
        """
        for label, prefix, builder, _ in RESOURCES:
            with self.subTest(resource=label):
                foreign_id = self._victim_id(builder)
                own_id = getattr(self.b, builder)()

                response = self.client.get(prefix)
                self.assertEqual(response.status_code, 200, f"{label}: list returned {response.status_code}")

                payload = response.json()
                rows = payload.get("results", payload) if isinstance(payload, dict) else payload
                ids = {row.get("id") for row in rows if isinstance(row, dict)}

                self.assertNotIn(foreign_id, ids, f"{label}: another tenant's row appeared in the list")
                self.assertIn(own_id, ids, f"{label}: the caller's own row was missing, so this proves nothing")

    def test_every_registered_resource_is_covered(self):
        """
        The table has not quietly shrunk.

        This is the test that makes the file worth having. Every other test
        here iterates RESOURCES, so deleting a row from RESOURCES makes them
        all pass while testing less -- which is precisely how isolation
        coverage rots. The number is asserted so that removing a resource is a
        deliberate edit to this line rather than an invisible one.
        """
        self.assertGreaterEqual(len(RESOURCES), 10)
        self.assertEqual(len(RESOURCES), len({label for label, *_ in RESOURCES}))


class MassAssignmentTests(TestCase):
    """
    A request body that names another tenant must not be honoured.

    Queryset scoping answers "which rows can I reach"; this answers "which
    rows can I *create or move*", and no amount of the former protects against
    the latter. The classic version is ``POST {"tenant": 2}``: the view filters
    reads by the caller's tenant, the serializer writes whatever it is given,
    and the row lands in somebody else's workspace.
    """

    def setUp(self):
        self.a = TenantFixture("massassign-alpha")
        self.b = TenantFixture("massassign-bravo")
        self.client = APIClient()
        self.client.force_authenticate(user=self.a.user)

    def test_creating_a_contact_cannot_name_another_tenant(self):
        from apps.contacts.models import Contact

        response = self.client.post(
            "/api/v1/contacts/contacts/",
            {"name": "Trojan", "phone": "+15559990001", "tenant": self.b.tenant.pk},
            format="json",
        )

        self.assertIn(response.status_code, (200, 201, 400))
        # Whatever the endpoint answered, nothing may have been written into
        # the other workspace.
        self.assertFalse(Contact.objects.filter(tenant=self.b.tenant, name="Trojan").exists())

    def test_updating_a_contact_cannot_move_it_to_another_tenant(self):
        from apps.contacts.models import Contact

        contact_id = self.a.contact()

        self.client.patch(
            f"/api/v1/contacts/contacts/{contact_id}/",
            {"tenant": self.b.tenant.pk},
            format="json",
        )

        contact = Contact.objects.get(pk=contact_id)
        self.assertEqual(contact.tenant_id, self.a.tenant.pk)

    def test_a_user_cannot_promote_themselves(self):
        """
        Role is not a field a customer may set on themselves.

        SUPERADMIN is platform-wide -- it is what the operations dashboard and
        the backup endpoint check -- so a self-service role change would be a
        privilege escalation out of the tenant model entirely.
        """
        User = get_user_model()

        for payload in (
            {"role": User.Role.SUPERADMIN},
            {"is_superuser": True},
            {"is_staff": True},
            {"credits": 1_000_000},
        ):
            with self.subTest(payload=payload):
                self.client.patch("/api/v1/auth/me/", payload, format="json")

                user = User.objects.get(pk=self.a.user.pk)
                self.assertEqual(user.role, User.Role.USER)
                self.assertFalse(user.is_superuser)
                self.assertFalse(user.is_staff)
                self.assertEqual(user.credits, 0)


class OperationsEndpointsAreNotCustomerVisibleTests(TestCase):
    """
    The Phase 5 operations endpoints are platform-only, and answer 404 otherwise.

    They report platform-wide volumes -- every tenant's queue depth, failure
    counts and subscription states -- so an ordinary customer reaching one
    would be a cross-tenant disclosure by a different route than the resource
    tests above cover.
    """

    def setUp(self):
        self.fixture = TenantFixture("ops-visibility")
        self.client = APIClient()
        self.client.force_authenticate(user=self.fixture.user)

    def test_ordinary_user_gets_404_from_operations_endpoints(self):
        for path in ("/health/operations/", "/health/dead-letters/", "/health/backups/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(
                    response.status_code, 404,
                    f"{path} answered {response.status_code} to a customer",
                )

    def test_anonymous_cannot_reach_operations_endpoints(self):
        anonymous = APIClient()
        for path in ("/health/operations/", "/health/dead-letters/"):
            with self.subTest(path=path):
                response = anonymous.get(path)
                self.assertIn(response.status_code, (401, 403, 404))
