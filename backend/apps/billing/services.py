"""
Checkout, verification, activation and reconciliation.

The rules this module exists to enforce, in the order they matter:

1. **The server prices the order.** `create_checkout` reads the plan from the
   database and writes the amount into the `Checkout` row. Nothing in the
   request influences it -- there is no amount field to send.

2. **A payment activates nothing until the provider confirms it.** Both entry
   points (the browser callback and the webhook) end at `_settle`, which acts
   only on a `PaymentResult` whose `captured` flag came from the provider's
   own API or a signature-verified webhook body.

3. **The amount and currency must match what was quoted.** A confirmed payment
   for the wrong amount is a *refusal*, recorded loudly, never a partial
   activation. This is the check that makes tampering pointless rather than
   merely difficult.

4. **Everything is idempotent through the database, not through a check.**
   Webhook redelivery is concurrent; a `if already_processed: return` loses
   that race. `WebhookEvent(provider, event_id)` and `Payment(checkout,
   status=CAPTURED)` are unique constraints, and losing the insert is the
   normal, expected way a duplicate is handled.

5. **A subscription is updated, never duplicated.** `Subscription.tenant` is a
   OneToOne, so there is structurally one per workspace; activation moves the
   plan and the period on that row.
"""

import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from datetime import timedelta

from django.utils import timezone

from apps.audit.services import log_event
from apps.onboarding import services as onboarding
from apps.onboarding.models import FunnelEvent
from apps.subscriptions.models import Plan, Subscription, SubscriptionNotice

from . import coupons
from .models import Checkout, Coupon, Payment, WebhookEvent
# Only what this module calls. The provider exception types are caught by
# apps.billing.views, which imports them from the package directly -- a
# service that re-exported them would be a second place to keep in step.
from .providers import get_provider, payments_enabled, require_live_provider

logger = logging.getLogger("sendstack")

#: How long a checkout stays payable. Long enough for somebody to finish a
#: bank redirect and a one-time password; short enough that a stale quote
#: cannot be paid months later at a price that has since changed.
CHECKOUT_TTL_MINUTES = 60


class BillingError(Exception):
    """Something about this request was wrong. The message is safe to show."""


# ---------------------------------------------------------------------------
# Choosing what to sell
# ---------------------------------------------------------------------------

def purchasable_plans(tenant):
    """
    The plans this workspace may actually buy, resolved server-side.

    A customer of a reseller sees **their reseller's** plans; a direct
    customer of the platform sees the platform's public ones. That is the
    reseller commercial model expressed as a query, and it is why a plan id
    from the browser is never enough on its own -- see `resolve_plan`.
    """
    parent_id = getattr(tenant, "parent_id", None)
    queryset = Plan.objects.filter(is_active=True, is_public=True)
    if parent_id:
        reseller_plans = queryset.filter(owner_id=parent_id)
        if reseller_plans.exists():
            return reseller_plans.order_by("display_order", "price_cents", "name")
    return queryset.filter(owner__isnull=True).order_by("display_order", "price_cents", "name")


def resolve_plan(tenant, plan_id):
    """
    The plan behind an id from the browser, or a refusal.

    Filtered through `purchasable_plans`, so an id naming a plan this
    workspace is not offered -- another reseller's tier, a private
    negotiated price, an inactive one -- resolves to nothing. Checking
    ownership *here* rather than trusting the id is what stops one customer
    buying another reseller's plan at that reseller's price.
    """
    try:
        plan_id = int(plan_id)
    except (TypeError, ValueError):
        raise BillingError("Choose a plan.") from None

    plan = purchasable_plans(tenant).filter(pk=plan_id).first()
    if plan is None:
        # Deliberately the same message as "no such plan": whether a given
        # plan id exists somewhere else in the database is not this
        # customer's business.
        raise BillingError("That plan is not available on your account.")
    return plan


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------

@transaction.atomic
def create_checkout(*, tenant, user, plan_id, coupon_code: str = "") -> Checkout:
    """
    Price an upgrade and open an order with the provider.

    The `Checkout` row is written **before** the provider is called and holds
    the amount the server decided. Everything downstream compares against this
    row, so a payment can only ever settle the order that was actually quoted.

    A coupon changes what that amount is and nothing else about how it is
    protected. The discount is computed here from the coupon row, frozen into
    `amount_minor`, and the plan is left alone -- so `_settle`'s amount check
    guards a discounted order with no new code and, more importantly, no new
    gap. A browser that sends `amount` or `discount` is ignored: the only thing
    it may send is a string to look up.
    """
    provider = require_live_provider()
    plan = resolve_plan(tenant, plan_id)

    list_amount = int(plan.price_cents)
    if list_amount <= 0:
        raise BillingError("That plan has no price set. Please contact support.")

    coupon = None
    discount = 0
    if coupon_code:
        # A bad code fails the whole request rather than being quietly dropped
        # and the full price charged. Somebody who typed a code and was billed
        # the list amount anyway has been overcharged as far as they are
        # concerned, and they are right.
        #
        # Re-raised as BillingError so the view's existing handler renders it:
        # coupons.py cannot import this module (services imports it), so the
        # translation happens here rather than through a shared base class.
        try:
            coupon = coupons.resolve(tenant, coupon_code)
            if coupon.kind == Coupon.Kind.TRIAL_EXTENSION:
                raise coupons.CouponError(
                    "That code extends a free trial. It cannot be used to pay for a plan."
                )
            coupons.check(coupon, tenant=tenant, plan=plan)
        except coupons.CouponError as exc:
            raise BillingError(str(exc)) from exc
        discount = coupon.discount_for(list_amount)

    checkout = Checkout.objects.create(
        tenant=tenant,
        created_by=user,
        plan=plan,
        provider=provider.name,
        # Frozen from the plan at this moment. If the plan's price changes
        # tomorrow, this order is still for what was shown today -- and the
        # verification below compares against this, not against the plan.
        amount_minor=max(list_amount - discount, 0),
        list_amount_minor=list_amount,
        discount_minor=discount,
        currency=(plan.currency or getattr(settings, "BILLING_CURRENCY", "INR")).upper(),
        expires_at=timezone.now() + timedelta(minutes=CHECKOUT_TTL_MINUTES),
    )

    if coupon is not None:
        # Takes a slot against the coupon's limits, under a lock. Inside this
        # transaction, so a coupon that turns out to be exhausted takes the
        # whole checkout down with it rather than leaving an order priced with
        # a discount nobody was entitled to.
        try:
            coupons.reserve(tenant=tenant, coupon=coupon, checkout=checkout, discount_minor=discount)
        except coupons.CouponError as exc:
            raise BillingError(str(exc)) from exc

    if checkout.amount_minor <= 0:
        # A coupon that makes a plan free is a legitimate thing to configure
        # and an illegitimate thing to send to a payment provider, which will
        # reject a zero-value order in a way the customer will read as a bug.
        raise BillingError(
            "That code covers the whole price of this plan. Please contact support to apply it."
        )

    session = provider.create_checkout(checkout=checkout)
    checkout.provider_order_id = session.provider_order_id
    checkout.status = Checkout.Status.PENDING
    # `public_params` is what the browser needs to open the hosted page. It is
    # the provider adapter's job to have put nothing secret in it; see
    # PaymentProvider.public_config.
    checkout.metadata = {"public_params": session.public_params}
    checkout.save(update_fields=["provider_order_id", "status", "metadata", "updated_at"])

    onboarding.record(FunnelEvent.Name.UPGRADE_STARTED, tenant=tenant, plan=plan.name)
    log_event(
        request=None, action="billing.checkout_created", tenant=tenant,
        object_type="billing.Checkout", object_id=checkout.pk,
        metadata={"plan": plan.name, "amount_minor": checkout.amount_minor, "currency": checkout.currency},
    )
    return checkout


def verify_payment(*, checkout, payload: dict) -> Payment:
    """
    Settle a checkout from the browser's return callback.

    This is the path that runs when the customer is redirected back with
    "payment successful". **The callback is not what activates anything.** It
    names a payment; the provider adapter verifies the signature and then asks
    the provider's own API what that payment's status and amount really are,
    and `_settle` acts on that answer alone.

    A customer who never returns to the browser is settled by the webhook
    instead, or by reconciliation. All three routes end in the same function.
    """
    provider = get_provider(checkout.provider)
    result = provider.verify_payment(checkout=checkout, payload=payload)
    return _settle(checkout, result, source="callback")


# ---------------------------------------------------------------------------
# Settlement -- the one path that can activate a plan
# ---------------------------------------------------------------------------

def _settle(checkout, result, *, source: str) -> Payment:
    """
    Turn a provider-confirmed payment into a subscription, exactly once.

    Every guard here is a refusal to activate, and each one is a real attack
    or a real accident:

    * amount or currency mismatch -- a tampered or replayed cheaper payment;
    * an uncaptured payment -- an authorisation the bank may still release;
    * a duplicate capture -- a webhook redelivered, or a callback racing it.
    """
    if not result.captured:
        payment = _record_payment(checkout, result, Payment.Status.FAILED)
        _mark_failed(checkout, result.failure_reason or "The payment did not complete.")
        logger.info(
            "Checkout %s: payment %s reported %s via %s.",
            checkout.pk, result.provider_payment_id, result.raw_status or "not captured", source,
        )
        return payment

    # **The amount check.** Compared against the checkout row, which was
    # written from the plan before the customer ever reached the provider.
    if int(result.amount_minor) != int(checkout.amount_minor) or (
        result.currency and result.currency.upper() != checkout.currency.upper()
    ):
        # Logged as an amount mismatch with both figures, because this is
        # either tampering or a genuine pricing bug and an operator needs to
        # be able to tell which. No customer data in the line.
        logger.error(
            "Checkout %s: amount mismatch. Quoted %s %s, provider reported %s %s. Not activating.",
            checkout.pk, checkout.amount_minor, checkout.currency,
            result.amount_minor, result.currency,
        )
        payment = _record_payment(checkout, result, Payment.Status.FAILED)
        _mark_failed(checkout, "The amount paid did not match the amount quoted.")
        log_event(
            request=None, action="billing.amount_mismatch", tenant=checkout.tenant,
            object_type="billing.Checkout", object_id=checkout.pk,
            metadata={
                "expected_minor": checkout.amount_minor, "expected_currency": checkout.currency,
                "received_minor": result.amount_minor, "received_currency": result.currency,
                "source": source,
            },
        )
        raise BillingError(
            "The payment did not match the amount for this plan and has not been applied. "
            "Nothing has been charged to your plan -- please contact support."
        )

    with transaction.atomic():
        # Lock the checkout for the duration. Two settlements racing -- the
        # browser callback and the webhook, which genuinely happens on nearly
        # every payment -- serialise here, and the second finds the work done.
        locked = Checkout.objects.select_for_update().get(pk=checkout.pk)

        existing = Payment.objects.filter(
            checkout=locked, status=Payment.Status.CAPTURED,
        ).first()
        if existing is not None:
            logger.info(
                "Checkout %s was already settled by payment %s; %s delivery ignored.",
                locked.pk, existing.provider_payment_id, source,
            )
            return existing

        payment = _record_payment(locked, result, Payment.Status.CAPTURED)
        locked.status = Checkout.Status.PAID
        locked.save(update_fields=["status", "updated_at"])

        # The reservation taken at checkout becomes a real redemption. Inside
        # the same transaction as the activation, so a coupon can never be
        # marked used against a subscription that did not start -- and,
        # filtering on RESERVED, the webhook and the callback both arriving
        # settle it exactly once.
        coupons.confirm(locked)

        subscription = activate_subscription(locked)

    onboarding.record(
        FunnelEvent.Name.PAYMENT_COMPLETED, tenant=checkout.tenant, plan=checkout.plan.name,
    )
    log_event(
        request=None, action="billing.payment_captured", tenant=checkout.tenant,
        object_type="billing.Payment", object_id=payment.pk,
        metadata={
            "plan": checkout.plan.name, "amount_minor": payment.amount_minor,
            "currency": payment.currency, "source": source,
        },
    )
    if subscription is not None and SubscriptionNotice.claim(
        subscription, SubscriptionNotice.Kind.SUBSCRIPTION_ACTIVATED, str(payment.pk),
    ):
        notify_workspace(subscription, SubscriptionNotice.Kind.SUBSCRIPTION_ACTIVATED)
    return payment


def _record_payment(checkout, result, status) -> Payment:
    """
    Write the payment row, or return the one that is already there.

    The unique constraint on `(provider, provider_payment_id)` does the
    de-duplication, so a redelivered webhook lands here and gets the original
    row back rather than a second one or an exception.
    """
    try:
        with transaction.atomic():
            return Payment.objects.create(
                checkout=checkout,
                tenant=checkout.tenant,
                provider=checkout.provider,
                provider_payment_id=result.provider_payment_id,
                amount_minor=int(result.amount_minor),
                currency=(result.currency or checkout.currency).upper(),
                status=status,
                failure_reason=result.failure_reason[:255],
                captured_at=timezone.now() if status == Payment.Status.CAPTURED else None,
            )
    except IntegrityError:
        existing = Payment.objects.filter(
            provider=checkout.provider, provider_payment_id=result.provider_payment_id,
        ).first()
        if existing is not None:
            return existing
        raise


def _mark_failed(checkout, reason: str) -> None:
    """
    Note a failed attempt without closing the door on a retry.

    The checkout stays payable: a customer whose first card is declined
    should be able to try another one against the same quoted price, and
    forcing them back through plan selection loses the sale and the price.
    """
    if checkout.is_open:
        checkout.metadata = {**(checkout.metadata or {}), "last_failure": reason[:255]}
        checkout.save(update_fields=["metadata", "updated_at"])


@transaction.atomic
def activate_subscription(checkout) -> Subscription:
    """
    Put the workspace on the plan it just paid for.

    Updates the one subscription row this tenant has -- never creates a second
    one, because `Subscription.tenant` is a OneToOne and a workspace on two
    plans has no defined entitlements.

    A period is *started fresh* rather than extended from the old end date.
    Extending is right for a renewal of the same plan and wrong for an upgrade
    (it would hand a customer the new plan's allowance for a period they paid
    for at the old plan's price), and this system's payments are one-off
    orders rather than provider-managed recurring ones. Renewal handling is
    documented as deferred; see docs/BILLING.md.
    """
    now = timezone.now()
    plan = checkout.plan

    subscription = Subscription.objects.select_for_update().filter(tenant_id=checkout.tenant_id).first()
    previous_plan = subscription.plan.name if subscription else None

    if subscription is None:
        subscription = Subscription(tenant_id=checkout.tenant_id)

    subscription.plan = plan
    subscription.status = Subscription.Status.ACTIVE
    subscription.current_period_start = now
    subscription.current_period_end = now + plan.period_length()
    subscription.cancelled_at = None
    # A paid subscription is not a trial any more. Cleared rather than left
    # behind, so `is_trial` and the trial banner cannot both be true of a
    # paying customer.
    subscription.trial_ends_at = None
    subscription.save()

    action = "subscription.upgraded" if previous_plan else "subscription.started"
    log_event(
        request=None, action=action, tenant=checkout.tenant,
        object_type="subscription", object_id=subscription.pk,
        metadata={"from": previous_plan, "to": plan.name, "checkout": checkout.pk},
    )
    logger.info(
        "Tenant %s activated on plan '%s' (was %s).", checkout.tenant_id, plan.name, previous_plan or "none",
    )
    return subscription


def notify_workspace(subscription, kind) -> None:
    try:
        from apps.subscriptions.notices import send_billing_notice

        send_billing_notice(subscription, kind)
    except Exception:  # noqa: BLE001 - a mail failure must not undo a paid activation
        logger.exception("Could not deliver the %s notice for subscription %s.", kind, subscription.pk)


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

def handle_webhook(*, provider_name: str, body: bytes, headers) -> WebhookEvent:
    """
    Take delivery of one provider callback.

    The order of operations is the security property:

    1. verify the signature over the **raw bytes**, before parsing;
    2. claim the event id, so a concurrent redelivery cannot also process it;
    3. map to a local checkout through **our own** order id, never through a
       tenant id in the payload;
    4. settle.

    A signature failure raises before step 2, so a forged event never even
    gets a row -- there is no value in letting an attacker fill this table.
    """
    provider = get_provider(provider_name)
    result = provider.handle_webhook(body=body, headers=headers)

    # Claim the event. Losing this insert means another delivery of the same
    # event is already being processed or has been -- the expected outcome for
    # a retry, and not an error.
    try:
        with transaction.atomic():
            event = WebhookEvent.objects.create(
                provider=provider.name,
                event_id=result.event_id,
                event_type=result.event_type[:64],
                payload=result.payload,
            )
    except IntegrityError:
        existing = WebhookEvent.objects.filter(
            provider=provider.name, event_id=result.event_id,
        ).first()
        logger.info("Webhook %s/%s already recorded; ignoring the redelivery.", provider.name, result.event_id)
        return existing

    # Asked of the adapter rather than imported from one. A generic service
    # that has to `import razorpay` to find out what it handles is not
    # provider-agnostic, whatever the interface above it looks like.
    if result.event_type not in getattr(provider, "handled_events", ()):
        return _close_event(event, WebhookEvent.Status.IGNORED, "Event type is not handled.")

    # **Tenant mapping goes through our own row.** The payload's `notes` carry
    # a tenant id we put there, and it is deliberately not read: anything in
    # the payload is only as trustworthy as the signature, and the signature
    # says the message came from the provider, not that the provider was told
    # the truth about which tenant to bill.
    checkout = Checkout.objects.filter(
        provider=provider.name, provider_order_id=result.provider_order_id,
    ).select_related("tenant", "plan").first()
    if checkout is None:
        return _close_event(
            event, WebhookEvent.Status.IGNORED, "No local checkout matches this order.",
        )

    event.checkout = checkout
    event.tenant = checkout.tenant
    event.save(update_fields=["checkout", "tenant"])

    if result.payment is None:
        return _close_event(event, WebhookEvent.Status.IGNORED, "The event carried no payment.")

    try:
        _settle(checkout, result.payment, source="webhook")
    except BillingError as exc:
        # An amount mismatch, recorded on the event so reconciliation and the
        # operator can both see it. Not re-raised: the provider gets its 200
        # and stops retrying an event that will never succeed.
        return _close_event(event, WebhookEvent.Status.FAILED, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Webhook %s/%s failed while settling.", provider.name, result.event_id)
        return _close_event(event, WebhookEvent.Status.FAILED, f"{type(exc).__name__}: {exc}"[:2000])

    return _close_event(event, WebhookEvent.Status.PROCESSED, "")


def _close_event(event, status, error) -> WebhookEvent:
    event.status = status
    event.processed_at = timezone.now()
    event.error = error[:2000]
    event.save(update_fields=["status", "processed_at", "error"])
    return event


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def reconcile_checkout(checkout) -> Payment | None:
    """
    Ask the provider what became of an order this system never saw settled.

    The failure this exists for: the provider takes the money, and then the
    webhook is lost and the customer closes the tab before the callback
    fires. Locally the checkout is PENDING for ever and a paying customer has
    no plan. Nothing about that is visible from inside this system, which is
    why the fix has to be to go and ask.

    Deliberately provider-first and one-way: it can turn a pending checkout
    into a paid one, and it can never turn a paid one into anything else.
    """
    if checkout.status == Checkout.Status.PAID:
        return Payment.objects.filter(checkout=checkout, status=Payment.Status.CAPTURED).first()
    if not checkout.provider_order_id:
        return None

    provider = get_provider(checkout.provider)
    fetch_order_payments = getattr(provider, "fetch_order_payments", None)
    if fetch_order_payments is None:
        logger.info("Provider %s cannot be reconciled by order.", provider.name)
        return None

    for result in fetch_order_payments(checkout.provider_order_id):
        if result.captured:
            return _settle(checkout, result, source="reconciliation")
    return None


def expire_stale_checkouts(*, now=None) -> int:
    """
    Close orders nobody paid.

    Only ever touches checkouts with **no captured payment**, so an order that
    was paid and whose webhook arrived late is never expired out from under
    the payment that settles it.
    """
    now = now or timezone.now()
    stale = Checkout.objects.filter(
        status__in=(Checkout.Status.CREATED, Checkout.Status.PENDING), expires_at__lt=now,
    ).exclude(payments__status=Payment.Status.CAPTURED)

    # Give back the coupon slots these orders were holding. This is the other
    # half of reserving at checkout creation: without it, a promotion limited
    # to a hundred redemptions would be exhausted by a hundred people who
    # opened the payment page and closed it. Done before the update, while the
    # rows can still be selected by status.
    from .models import CouponRedemption

    CouponRedemption.objects.filter(
        checkout__in=stale, status=CouponRedemption.Status.RESERVED,
    ).update(status=CouponRedemption.Status.RELEASED, settled_at=now)

    return stale.update(status=Checkout.Status.EXPIRED, updated_at=now)


def billing_state(tenant) -> dict:
    """
    Everything the billing page renders, decided here rather than in the browser.

    Includes `payments_enabled` so the UI shows an upgrade button only when
    pressing it would actually work -- the alternative being a button that
    exists and then explains it does not.
    """
    from apps.subscriptions import services as entitlements
    from apps.subscriptions import trials

    subscription = entitlements.get_active_subscription(tenant)
    quota = entitlements.quota_state(getattr(tenant, "pk", tenant))

    return {
        "payments_enabled": payments_enabled(),
        "currency": getattr(settings, "BILLING_CURRENCY", "INR"),
        "trials_enabled": trials.trials_enabled(),
        "trial_days": trials.trial_days(),
        "subscription": _subscription_block(subscription),
        "usage": quota,
    }


def _subscription_block(subscription) -> dict | None:
    if subscription is None:
        return None
    period_start, period_end = subscription.current_period()
    return {
        "status": subscription.status,
        "access_state": subscription.access_state(),
        "plan": subscription.plan.name,
        "plan_id": subscription.plan_id,
        "interval": subscription.plan.interval,
        "price_minor": subscription.plan.price_cents,
        "currency": subscription.plan.currency,
        "current_period_start": period_start,
        "current_period_end": period_end,
        "cancelled_at": subscription.cancelled_at,
        "is_trial": subscription.is_trial,
        "trial_ends_at": subscription.trial_ends_at,
        "trial_days_remaining": subscription.trial_days_remaining(),
    }
