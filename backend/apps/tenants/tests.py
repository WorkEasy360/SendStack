from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .models import Tenant, TenantBranding


class TenantTests(TestCase):
    def test_slug_is_generated_from_the_name(self):
        tenant = Tenant.objects.create(name="Acme Trading", kind=Tenant.Kind.CUSTOMER)
        self.assertEqual(tenant.slug, "acme-trading")

    def test_descendants_know_their_ancestors(self):
        platform = Tenant.objects.create(name="Platform", kind=Tenant.Kind.PLATFORM)
        reseller = Tenant.objects.create(name="Reseller", kind=Tenant.Kind.RESELLER, parent=platform)
        customer = Tenant.objects.create(name="Customer", kind=Tenant.Kind.CUSTOMER, parent=reseller)

        self.assertTrue(customer.is_descendant_of(platform))
        self.assertTrue(customer.is_descendant_of(reseller))
        self.assertFalse(reseller.is_descendant_of(customer))


class BrandingResolutionTests(TestCase):
    def setUp(self):
        self.platform = Tenant.objects.create(name="Platform", kind=Tenant.Kind.PLATFORM)
        self.reseller = Tenant.objects.create(name="Reseller", kind=Tenant.Kind.RESELLER, parent=self.platform)
        self.customer = Tenant.objects.create(name="Customer", kind=Tenant.Kind.CUSTOMER, parent=self.reseller)

    def test_a_tenant_with_no_branding_anywhere_gets_the_platform_defaults(self):
        self.assertEqual(TenantBranding.resolve(self.customer), TenantBranding.DEFAULTS)

    def test_branding_is_inherited_from_an_ancestor(self):
        TenantBranding.objects.create(tenant=self.reseller, product_name="Acme Chat", primary_color="#0f766e")

        resolved = TenantBranding.resolve(self.customer)
        self.assertEqual(resolved["product_name"], "Acme Chat")
        self.assertEqual(resolved["primary_color"], "#0f766e")

    def test_each_field_is_inherited_on_its_own(self):
        """A customer overriding the name still shows their reseller's colour."""
        TenantBranding.objects.create(tenant=self.reseller, product_name="Acme Chat", primary_color="#0f766e")
        TenantBranding.objects.create(tenant=self.customer, product_name="Bright Co")

        resolved = TenantBranding.resolve(self.customer)
        self.assertEqual(resolved["product_name"], "Bright Co")
        self.assertEqual(resolved["primary_color"], "#0f766e")

    def test_a_blank_override_does_not_shadow_an_ancestor(self):
        """Blank means inherit, not empty -- otherwise clearing a field breaks the page."""
        TenantBranding.objects.create(tenant=self.reseller, product_name="Acme Chat")
        TenantBranding.objects.create(tenant=self.customer, product_name="")

        self.assertEqual(TenantBranding.resolve(self.customer)["product_name"], "Acme Chat")

    def test_a_parent_cycle_cannot_hang_resolution(self):
        """Bad data must not turn every page load into an infinite loop."""
        self.platform.parent = self.customer
        self.platform.save(update_fields=["parent"])

        self.assertEqual(TenantBranding.resolve(self.customer), TenantBranding.DEFAULTS)

    def test_a_blank_custom_domain_is_stored_as_null_so_many_may_have_none(self):
        TenantBranding.objects.create(tenant=self.reseller, custom_domain="")
        TenantBranding.objects.create(tenant=self.customer, custom_domain="")

        self.assertEqual(TenantBranding.objects.filter(custom_domain__isnull=True).count(), 2)


class BrandingAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = Tenant.objects.create(name="Reseller", kind=Tenant.Kind.RESELLER)
        user_model = get_user_model()
        self.reseller = user_model.objects.create_user(
            username="reseller", password="pw", tenant=self.tenant, role=user_model.Role.RESELLER,
        )
        self.member = user_model.objects.create_user(
            username="member", password="pw", tenant=self.tenant, role=user_model.Role.USER,
        )

    def test_a_reseller_can_set_their_branding(self):
        self.client.force_authenticate(self.reseller)
        response = self.client.patch(
            reverse("tenant-branding"), {"product_name": "Acme Chat", "primary_color": "#0F766E"}, format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["product_name"], "Acme Chat")
        self.assertEqual(response.data["primary_color"], "#0f766e")
        self.assertEqual(response.data["effective"]["product_name"], "Acme Chat")

    def test_an_ordinary_user_may_read_but_not_change_branding(self):
        self.client.force_authenticate(self.member)

        self.assertEqual(self.client.get(reverse("tenant-branding")).status_code, 200)
        self.assertEqual(
            self.client.patch(reverse("tenant-branding"), {"product_name": "Nope"}, format="json").status_code, 403,
        )

    def test_a_colour_that_is_not_a_hex_value_is_refused(self):
        self.client.force_authenticate(self.reseller)
        response = self.client.patch(reverse("tenant-branding"), {"primary_color": "teal"}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("primary_color", response.data)

    def test_a_pasted_url_is_reduced_to_a_bare_host(self):
        self.client.force_authenticate(self.reseller)
        response = self.client.patch(
            reverse("tenant-branding"), {"custom_domain": "https://App.Acme.io/login"}, format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["custom_domain"], "app.acme.io")

    def test_the_login_page_gets_branding_for_a_host_without_signing_in(self):
        TenantBranding.objects.create(tenant=self.tenant, product_name="Acme Chat", custom_domain="app.acme.io")

        response = self.client.get(reverse("tenant-branding-public"), {"host": "app.acme.io"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["product_name"], "Acme Chat")

    def test_an_unknown_host_gets_the_platform_defaults_rather_than_a_404(self):
        """Never confirm whether a tenant exists to an unauthenticated caller."""
        response = self.client.get(reverse("tenant-branding-public"), {"host": "nobody.example.com"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["product_name"], TenantBranding.DEFAULTS["product_name"])

    def test_effective_branding_is_readable_by_any_signed_in_user(self):
        TenantBranding.objects.create(tenant=self.tenant, product_name="Acme Chat")
        self.client.force_authenticate(self.member)

        response = self.client.get(reverse("tenant-branding-effective"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["product_name"], "Acme Chat")
