from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant

from .models import CreditTransaction
from .services import (
    CreditError,
    InsufficientCreditsError,
    assign_credits,
    consume_credit,
    deduct_credits,
    release_credits,
    reserve_credits,
)

User = get_user_model()


class HierarchyFixture(TestCase):
    def setUp(self):
        self.platform = Tenant.objects.create(name="Platform", slug="platform-credits", kind=Tenant.Kind.PLATFORM)
        self.superadmin = User.objects.create_user(username="superadmin-credits", password="x", role=User.Role.SUPERADMIN, tenant=self.platform)

        self.reseller_tenant = Tenant.objects.create(name="Partner", slug="partner-credits", kind=Tenant.Kind.RESELLER, parent=self.platform)
        self.reseller = User.objects.create_user(username="reseller-credits", password="x", role=User.Role.RESELLER, tenant=self.reseller_tenant)

        self.user_tenant = Tenant.objects.create(name="Client", slug="client-credits", kind=Tenant.Kind.CUSTOMER, parent=self.reseller_tenant)
        self.user = User.objects.create_user(username="user-credits", password="x", role=User.Role.USER, tenant=self.user_tenant)

        self.other_reseller_tenant = Tenant.objects.create(name="Other Partner", slug="other-partner-credits", kind=Tenant.Kind.RESELLER, parent=self.platform)
        self.other_reseller = User.objects.create_user(username="other-reseller-credits", password="x", role=User.Role.RESELLER, tenant=self.other_reseller_tenant)


class AssignCreditsServiceTests(HierarchyFixture):
    def test_superadmin_mints_credits_for_reseller(self):
        debit_txn, credit_txn = assign_credits(actor=self.superadmin, target=self.reseller, amount=500, reference="onboarding")

        self.assertIsNone(debit_txn)
        self.reseller.refresh_from_db()
        self.assertEqual(self.reseller.credits, 500)
        self.assertEqual(credit_txn.type, CreditTransaction.Type.GRANT)
        self.assertEqual(credit_txn.balance_before, 0)
        self.assertEqual(credit_txn.balance_after, 500)

    def test_reseller_transfers_from_own_balance_to_user(self):
        assign_credits(actor=self.superadmin, target=self.reseller, amount=500)

        debit_txn, credit_txn = assign_credits(actor=self.reseller, target=self.user, amount=200, reference="monthly")

        self.reseller.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(self.reseller.credits, 300)
        self.assertEqual(self.user.credits, 200)
        self.assertEqual(debit_txn.type, CreditTransaction.Type.TRANSFER)
        self.assertEqual(debit_txn.balance_after, 300)
        self.assertEqual(credit_txn.balance_after, 200)

    def test_reseller_can_grant_without_deducting_own_balance(self):
        assign_credits(actor=self.superadmin, target=self.reseller, amount=50)

        debit_txn, credit_txn = assign_credits(actor=self.reseller, target=self.user, amount=200, reference="promo", deduct=False)

        self.assertIsNone(debit_txn)
        self.reseller.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(self.reseller.credits, 50)
        self.assertEqual(self.user.credits, 200)
        self.assertEqual(credit_txn.type, CreditTransaction.Type.GRANT)
        self.assertEqual(credit_txn.balance_after, 200)

    def test_reseller_cannot_assign_more_than_they_have(self):
        assign_credits(actor=self.superadmin, target=self.reseller, amount=10)

        with self.assertRaises(InsufficientCreditsError):
            assign_credits(actor=self.reseller, target=self.user, amount=11)

    def test_reseller_cannot_assign_to_another_reseller(self):
        assign_credits(actor=self.superadmin, target=self.reseller, amount=500)

        with self.assertRaises(CreditError):
            assign_credits(actor=self.reseller, target=self.other_reseller, amount=10)

    def test_reseller_cannot_assign_to_users_outside_their_tenant(self):
        outside_tenant = Tenant.objects.create(name="Outside", slug="outside-credits", kind=Tenant.Kind.CUSTOMER, parent=self.other_reseller_tenant)
        outsider = User.objects.create_user(username="outsider-credits", password="x", role=User.Role.USER, tenant=outside_tenant)
        assign_credits(actor=self.superadmin, target=self.reseller, amount=500)

        with self.assertRaises(CreditError):
            assign_credits(actor=self.reseller, target=outsider, amount=10)

    def test_user_cannot_assign_credits(self):
        with self.assertRaises(CreditError):
            assign_credits(actor=self.user, target=self.reseller, amount=10)


class DeductCreditsServiceTests(HierarchyFixture):
    def test_reseller_deducts_from_user_and_reclaims_to_own_balance(self):
        assign_credits(actor=self.superadmin, target=self.reseller, amount=500)
        assign_credits(actor=self.reseller, target=self.user, amount=200)

        debit_txn, credit_txn = deduct_credits(actor=self.reseller, target=self.user, amount=50, reference="correction")

        self.reseller.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 150)
        self.assertEqual(self.reseller.credits, 350)
        self.assertEqual(debit_txn.type, CreditTransaction.Type.REFUND)
        self.assertEqual(debit_txn.balance_after, 150)
        self.assertEqual(credit_txn.balance_after, 350)

    def test_superadmin_deducts_from_reseller(self):
        assign_credits(actor=self.superadmin, target=self.reseller, amount=500)

        deduct_credits(actor=self.superadmin, target=self.reseller, amount=100)

        self.reseller.refresh_from_db()
        self.assertEqual(self.reseller.credits, 400)

    def test_cannot_deduct_more_than_available_balance(self):
        assign_credits(actor=self.superadmin, target=self.reseller, amount=500)
        assign_credits(actor=self.reseller, target=self.user, amount=10)

        with self.assertRaises(InsufficientCreditsError):
            deduct_credits(actor=self.reseller, target=self.user, amount=11)

    def test_cannot_deduct_credits_that_are_reserved(self):
        assign_credits(actor=self.superadmin, target=self.reseller, amount=500)
        assign_credits(actor=self.reseller, target=self.user, amount=10)
        reserve_credits(user_id=self.user.pk, amount=4, reference="campaign:1")

        with self.assertRaises(InsufficientCreditsError):
            deduct_credits(actor=self.reseller, target=self.user, amount=7)

    def test_reseller_cannot_deduct_from_another_resellers_user(self):
        outside_tenant = Tenant.objects.create(name="Outside", slug="outside-deduct", kind=Tenant.Kind.CUSTOMER, parent=self.other_reseller_tenant)
        outsider = User.objects.create_user(username="outsider-deduct", password="x", role=User.Role.USER, tenant=outside_tenant)

        with self.assertRaises(CreditError):
            deduct_credits(actor=self.reseller, target=outsider, amount=1)

    def test_user_cannot_deduct_credits(self):
        with self.assertRaises(CreditError):
            deduct_credits(actor=self.user, target=self.reseller, amount=1)


class ReservationServiceTests(HierarchyFixture):
    def setUp(self):
        super().setUp()
        self.user.credits = 10
        self.user.save(update_fields=["credits"])

    def test_reserve_then_consume_reduces_both_balances(self):
        reserve_credits(user_id=self.user.pk, amount=3, reference="campaign:1")
        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 10)
        self.assertEqual(self.user.reserved_credits, 3)

        consume_credit(user_id=self.user.pk, reference="message:1")
        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 9)
        self.assertEqual(self.user.reserved_credits, 2)

    def test_release_returns_reservation_without_touching_balance(self):
        reserve_credits(user_id=self.user.pk, amount=3, reference="campaign:1")

        release_credits(user_id=self.user.pk, amount=3, reference="campaign:1:cancel")

        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 10)
        self.assertEqual(self.user.reserved_credits, 0)

    def test_cannot_reserve_more_than_available(self):
        reserve_credits(user_id=self.user.pk, amount=8, reference="campaign:1")

        with self.assertRaises(InsufficientCreditsError):
            reserve_credits(user_id=self.user.pk, amount=3, reference="campaign:2")

    def test_reserve_leaves_a_ledger_trail(self):
        reserve_credits(user_id=self.user.pk, amount=2, reference="campaign:1")
        consume_credit(user_id=self.user.pk, reference="message:1")
        release_credits(user_id=self.user.pk, amount=1, reference="message:2")

        types = list(CreditTransaction.objects.filter(account=self.user).order_by("created_at").values_list("type", flat=True))
        self.assertEqual(types, [CreditTransaction.Type.RESERVE, CreditTransaction.Type.CONSUME, CreditTransaction.Type.RELEASE])


class CreditsApiTests(HierarchyFixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_balance_endpoint_reflects_reservations(self):
        self.user.credits = 10
        self.user.reserved_credits = 4
        self.user.save(update_fields=["credits", "reserved_credits"])
        self.client.force_authenticate(user=self.user)

        response = self.client.get("/api/v1/credits/balance/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"credits": 10, "reserved_credits": 4, "available_credits": 6})

    def test_assign_endpoint_requires_reseller_or_superadmin(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post("/api/v1/credits/assign/", {"target_user_id": self.reseller.pk, "amount": 10}, format="json")

        self.assertEqual(response.status_code, 403)

    def test_assign_endpoint_grants_credits(self):
        self.client.force_authenticate(user=self.superadmin)

        response = self.client.post("/api/v1/credits/assign/", {"target_user_id": self.reseller.pk, "amount": 100}, format="json")

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["credits"], 100)

    def test_user_only_sees_own_transactions(self):
        assign_credits(actor=self.superadmin, target=self.reseller, amount=100)
        assign_credits(actor=self.reseller, target=self.user, amount=20)
        self.client.force_authenticate(user=self.user)

        response = self.client.get("/api/v1/credits/transactions/")

        accounts = {row["account"] for row in response.json()["results"]}
        self.assertEqual(accounts, {self.user.pk})

    def test_reseller_sees_own_and_managed_users_transactions(self):
        assign_credits(actor=self.superadmin, target=self.reseller, amount=100)
        assign_credits(actor=self.reseller, target=self.user, amount=20)
        self.client.force_authenticate(user=self.reseller)

        response = self.client.get("/api/v1/credits/transactions/")

        accounts = {row["account"] for row in response.json()["results"]}
        self.assertEqual(accounts, {self.reseller.pk, self.user.pk})
