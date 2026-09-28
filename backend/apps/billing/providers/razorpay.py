"""
Razorpay, spoken through the provider interface.

Chosen because this deployment prices in INR by default and Razorpay is the
ordinary answer for an India-first SaaS. Nothing above this module knows that;
swapping it for another gateway is writing a sibling of this file.

Implemented against Razorpay's HTTP API with `requests` and `hmac` rather than
the vendor SDK. Two reasons, and the second is the important one: the SDK adds
a dependency for what is three endpoints and one HMAC, and -- more to the
point -- signature verification is the security boundary of the whole payment
flow, so it is worth having it visible in this repository rather than
delegated to a version range.

**The two signatures, which are different and are often confused:**

* *Checkout callback*: HMAC-SHA256 of ``order_id|payment_id`` keyed by the
  API secret. This is what the browser hands back after a successful payment.
* *Webhook*: HMAC-SHA256 of the **raw request body** keyed by the separate
  webhook secret. Computing it over a re-serialised body instead of the bytes
  received is the classic way to make this check pass for nobody, or worse,
  for everybody.

Both are compared with `hmac.compare_digest`, because a signature check that
returns early on the first wrong byte is a signature check with a timing side
channel.
"""

import hashlib
import hmac
import json
import logging

import requests
from django.conf import settings

from .base import (
    CheckoutSession,
    PaymentError,
    PaymentNotConfigured,
    PaymentProvider,
    PaymentResult,
    SignatureInvalid,
    WebhookResult,
)

logger = logging.getLogger("sendstack")

API_BASE = "https://api.razorpay.com/v1"
TIMEOUT_SECONDS = 20

#: Webhook events this integration acts on. Anything else is recorded and
#: ignored -- an allow-list rather than a deny-list, so a new provider event
#: type cannot start driving behaviour nobody designed.
HANDLED_EVENTS = ("payment.captured", "payment.failed", "order.paid")


class RazorpayProvider(PaymentProvider):
    name = "razorpay"
    supports_checkout = True
    handled_events = HANDLED_EVENTS

    # -- Configuration ------------------------------------------------------

    @property
    def key_id(self) -> str:
        return (getattr(settings, "RAZORPAY_KEY_ID", "") or "").strip()

    @property
    def _key_secret(self) -> str:
        return (getattr(settings, "RAZORPAY_KEY_SECRET", "") or "").strip()

    @property
    def _webhook_secret(self) -> str:
        return (getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "") or "").strip()

    def is_configured(self) -> bool:
        """
        Both API credentials present. The webhook secret is checked separately,
        where it is used: a deployment can legitimately take payments through
        the browser callback before it has finished wiring webhooks up, and
        refusing checkout for that would be over-strict. Webhook *verification*
        is never skipped, though -- see `handle_webhook`.
        """
        return bool(self.key_id and self._key_secret)

    def public_config(self) -> dict:
        # The key id only. Razorpay publishes it and the browser cannot open a
        # checkout without it; the secret has no path out of this process.
        return {"key_id": self.key_id, "provider": self.name}

    def _auth(self) -> tuple:
        if not self.is_configured():
            raise PaymentNotConfigured("Razorpay credentials are not configured.")
        return (self.key_id, self._key_secret)

    # -- Checkout -----------------------------------------------------------

    def create_checkout(self, *, checkout) -> CheckoutSession:
        """
        Create the Razorpay order this checkout will be paid against.

        `receipt` carries our own checkout id so that a payment can always be
        traced back to the row that priced it, even by hand in the provider's
        dashboard. `notes` carries the tenant id for the same reason -- **and
        is never trusted on the way back in**: the webhook maps to a tenant
        through our own Checkout row, not through a note a caller could set.
        """
        payload = {
            "amount": int(checkout.amount_minor),
            "currency": checkout.currency,
            "receipt": f"checkout-{checkout.pk}",
            # Razorpay captures automatically rather than leaving the payment
            # authorised: an authorised-but-uncaptured payment that nobody
            # captures expires and the customer's money comes back days later
            # having bought them nothing.
            "payment_capture": 1,
            "notes": {"checkout_id": str(checkout.pk), "tenant_id": str(checkout.tenant_id)},
        }
        data = self._post("/orders", payload)
        order_id = data.get("id") or ""
        if not order_id:
            raise PaymentError("The payment provider did not return an order.")

        return CheckoutSession(
            provider_order_id=order_id,
            amount_minor=int(data.get("amount") or checkout.amount_minor),
            currency=data.get("currency") or checkout.currency,
            public_params={
                **self.public_config(),
                "order_id": order_id,
                "amount": int(data.get("amount") or checkout.amount_minor),
                "currency": data.get("currency") or checkout.currency,
            },
        )

    def verify_payment(self, *, checkout, payload: dict) -> PaymentResult:
        """
        Check the browser's callback, then ask Razorpay what really happened.

        Both, in that order, and neither alone is enough:

        * The signature proves the callback was produced by somebody holding
          our API secret, i.e. that it came from Razorpay and was not typed by
          the customer. It does **not** prove the payment was captured -- it is
          computed over two ids and nothing else.
        * The API fetch is what says whether the money actually moved, and for
          how much. It is the authority.

        Verifying only the signature is the mistake that lets a customer replay
        a genuine ₹1 test payment's callback against a ₹9,999 order.
        """
        order_id = str(payload.get("razorpay_order_id") or "")
        payment_id = str(payload.get("razorpay_payment_id") or "")
        signature = str(payload.get("razorpay_signature") or "")

        if not (order_id and payment_id and signature):
            raise SignatureInvalid("The payment confirmation was incomplete.")

        # The callback must be for *this* checkout. Without this, a caller
        # could present a valid signature from one of their own cheap orders
        # against somebody else's expensive one.
        if order_id != checkout.provider_order_id:
            raise SignatureInvalid("The payment confirmation does not match this order.")

        expected = self._sign(f"{order_id}|{payment_id}".encode("utf-8"), self._key_secret)
        if not hmac.compare_digest(expected, signature):
            raise SignatureInvalid("The payment confirmation could not be verified.")

        return self.fetch_payment(payment_id)

    def fetch_payment(self, payment_id: str) -> PaymentResult:
        """
        What the provider says about one payment. The authority on amount and status.

        Used both by the browser-callback path and by reconciliation, so there
        is one definition of "did this payment succeed" rather than two that
        can disagree.
        """
        data = self._get(f"/payments/{payment_id}")
        status = (data.get("status") or "").lower()
        return PaymentResult(
            provider_payment_id=data.get("id") or payment_id,
            provider_order_id=data.get("order_id") or "",
            amount_minor=int(data.get("amount") or 0),
            currency=(data.get("currency") or "").upper(),
            # "captured" and nothing else. "authorized" means the money is
            # held and not taken; treating it as paid would activate a
            # subscription that the bank later releases.
            captured=status == "captured",
            failure_reason=str(data.get("error_description") or "")[:255],
            raw_status=status,
        )

    # -- Webhooks -----------------------------------------------------------

    def handle_webhook(self, *, body: bytes, headers) -> WebhookResult:
        """
        Verify and parse a webhook. Nothing is parsed before the signature checks out.

        Refuses outright when no webhook secret is configured. An unsigned
        webhook endpoint is an unauthenticated "activate this subscription"
        endpoint, and there is no configuration in which accepting one is
        better than rejecting it.
        """
        secret = self._webhook_secret
        if not secret:
            raise PaymentNotConfigured("No webhook secret is configured; webhooks are refused.")

        signature = headers.get("X-Razorpay-Signature") or headers.get("HTTP_X_RAZORPAY_SIGNATURE") or ""
        if not signature:
            raise SignatureInvalid("The webhook carried no signature.")

        # Over the raw bytes as received. Re-serialising the parsed JSON would
        # change whitespace and key order and produce a different digest.
        expected = self._sign(body, secret)
        if not hmac.compare_digest(expected, str(signature)):
            raise SignatureInvalid("The webhook signature did not verify.")

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PaymentError("The webhook body was not valid JSON.") from exc

        event_type = str(data.get("event") or "")
        entities = (data.get("payload") or {})
        payment_entity = ((entities.get("payment") or {}).get("entity")) or {}
        order_entity = ((entities.get("order") or {}).get("entity")) or {}

        order_id = str(payment_entity.get("order_id") or order_entity.get("id") or "")

        payment = None
        if payment_entity:
            status = (payment_entity.get("status") or "").lower()
            payment = PaymentResult(
                provider_payment_id=str(payment_entity.get("id") or ""),
                provider_order_id=order_id,
                amount_minor=int(payment_entity.get("amount") or 0),
                currency=str(payment_entity.get("currency") or "").upper(),
                captured=status == "captured",
                failure_reason=str(payment_entity.get("error_description") or "")[:255],
                raw_status=status,
            )

        return WebhookResult(
            event_id=self._event_id(headers, data),
            event_type=event_type,
            provider_order_id=order_id,
            payment=payment,
            payload=data,
        )

    @staticmethod
    def _event_id(headers, data) -> str:
        """
        A stable id for this event, for the idempotency ledger.

        Razorpay sends ``X-Razorpay-Event-Id`` and that is used when present.
        The fallback is a digest of the payload rather than a random value:
        the whole purpose of this id is that a redelivery produces the *same*
        one, and a random id would make every retry look new -- which is
        exactly the double-activation this ledger exists to prevent.
        """
        header_id = headers.get("X-Razorpay-Event-Id") or headers.get("HTTP_X_RAZORPAY_EVENT_ID")
        if header_id:
            return str(header_id)[:128]
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    # -- HTTP ---------------------------------------------------------------

    @staticmethod
    def _sign(message: bytes, secret: str) -> str:
        return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

    def _post(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, json=payload)

    def _get(self, path: str) -> dict:
        return self._request("GET", path)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        auth = self._auth()
        try:
            response = requests.request(
                method, f"{API_BASE}{path}", auth=auth, timeout=TIMEOUT_SECONDS, **kwargs
            )
        except requests.RequestException as exc:
            # The exception is logged without the request, which carries the
            # auth header. `str(exc)` from requests can include the URL but
            # never the credentials.
            logger.exception("Razorpay %s %s could not be reached.", method, path)
            raise PaymentError("We could not reach the payment provider. Please try again.") from exc

        if response.status_code >= 400:
            # The provider's own message is logged for the operator and a
            # generic one is raised for the customer: a gateway error body can
            # name the merchant account and the failure reason in more detail
            # than a customer-facing string should carry.
            logger.error(
                "Razorpay %s %s returned %s: %s", method, path, response.status_code, response.text[:500],
            )
            raise PaymentError("The payment provider rejected that request.")

        try:
            return response.json()
        except ValueError as exc:
            raise PaymentError("The payment provider returned an unreadable response.") from exc

    # -- Reconciliation -----------------------------------------------------

    def fetch_order_payments(self, order_id: str) -> list:
        """
        Every payment attempt Razorpay has recorded against one order.

        The way out of the failure that browser callbacks and webhooks cannot
        cover between them: the money was taken, the webhook was lost, and the
        customer closed the tab. Nothing local knows anything is wrong, so the
        only fix is to ask the provider -- which is what this is for.

        Returns an empty list rather than raising when the provider cannot be
        reached, because reconciliation runs unattended and a gateway blip is
        a reason to try again later, not to alarm anybody.
        """
        try:
            data = self._get(f"/orders/{order_id}/payments")
        except PaymentError:
            logger.warning("Could not reconcile order %s: the provider was unreachable.", order_id)
            return []

        results = []
        for item in data.get("items") or []:
            status = (item.get("status") or "").lower()
            results.append(
                PaymentResult(
                    provider_payment_id=str(item.get("id") or ""),
                    provider_order_id=str(item.get("order_id") or order_id),
                    amount_minor=int(item.get("amount") or 0),
                    currency=str(item.get("currency") or "").upper(),
                    captured=status == "captured",
                    failure_reason=str(item.get("error_description") or "")[:255],
                    raw_status=status,
                )
            )
        return results
