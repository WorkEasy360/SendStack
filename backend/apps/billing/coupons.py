"""
Deciding whether a coupon code is worth anything, and to whom.

**Every rule in this module runs on the server, and none of it is reachable
from a request body.** The browser sends a string; it never sends a discount,
a plan, an amount or a redemption count. `quote` turns that string into a
number, `reserve` takes a slot, and the amount it produces is frozen onto the
`Checkout` row before the customer reaches the payment provider -- which means
the existing amount check in `services._settle` protects a discounted order
exactly as it protects an undiscounted one, with no new code and no new gap.

The awkward part of coupons is the usage limit, and it is worth saying why it
is done the way it is. Counting redemptions at capture is the obvious design
and it is wrong: fifty checkouts opened at once all see zero used, all pass,
and a coupon "limited to 100" is redeemed 150 times. Counting at *checkout
creation* fixes that but strands a slot every time somebody abandons a
payment. So a redemption is reserved, and released when the order dies --
three states rather than two, and `expire_stale_checkouts` sweeping the
abandoned ones. The lock in `reserve` is what makes the count itself safe
under concurrency; the DB constraint is what makes a retried request idempotent.
"""

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Coupon, CouponRedemption

logger = logging.getLogger("sendstack")

#: What a customer is told when a code does not work, whatever the reason.
#:
#: Deliberately one message for expired, exhausted, wrong-plan, wrong-reseller
#: and never-existed. The alternative leaks a working code list: "this code has
#: expired" tells somebody guessing that they guessed a real code, and "not
#: valid for this plan" tells them to try the other plan. An operator can see
#: the real reason in the log line; a customer with a genuine code gets it from
#: whoever gave it to them.
INVALID = "That code is not valid for this purchase."


class CouponError(Exception):
    """The code cannot be used. The message is safe to show a customer."""


def _available_coupons(tenant):
    """
    The coupons this workspace could possibly redeem.

    Mirrors `services.purchasable_plans` exactly, and for the same reason: a
    customer of a reseller lives in that reseller's commercial world. Their
    reseller's codes, or the platform's for a direct customer -- never both,
    and never another reseller's.
    """
    parent_id = getattr(tenant, "parent_id", None)
    if parent_id:
        return Coupon.objects.filter(owner_id=parent_id)
    return Coupon.objects.filter(owner__isnull=True)


def resolve(tenant, code: str) -> Coupon:
    """The coupon behind a code from the browser, or a refusal. Checks nothing else."""
    code = (code or "").strip().upper()
    if not code:
        raise CouponError("Enter a code.")
    coupon = _available_coupons(tenant).filter(code=code).first()
    if coupon is None:
        logger.info("Coupon %s was not found for tenant %s.", code, getattr(tenant, "pk", tenant))
        raise CouponError(INVALID)
    return coupon


def _redemptions_used(coupon, tenant_id=None) -> int:
    """How many slots are taken, counting reservations. Released ones do not count."""
    queryset = CouponRedemption.objects.filter(
        coupon=coupon, status__in=CouponRedemption.COUNTED,
    )
    if tenant_id is not None:
        queryset = queryset.filter(tenant_id=tenant_id)
    return queryset.count()


def check(coupon, *, tenant, plan=None, now=None) -> None:
    """
    Whether this workspace may use this coupon for this plan, right now.

    Raises `CouponError` with the deliberately vague message; the specific
    reason goes to the log, where an operator answering "why won't my code
    work" can find it.
    """
    now = now or timezone.now()
    tenant_id = getattr(tenant, "pk", tenant)

    if not coupon.is_live(now):
        logger.info("Coupon %s refused for tenant %s: outside its window or inactive.", coupon.code, tenant_id)
        raise CouponError(INVALID)

    if plan is not None:
        # `.exists()` rather than loading the set: the common case is no
        # restriction at all, and that must not cost a join.
        restricted = coupon.plans.exists()
        if restricted and not coupon.plans.filter(pk=plan.pk).exists():
            logger.info("Coupon %s refused for tenant %s: not valid for plan %s.", coupon.code, tenant_id, plan.pk)
            raise CouponError(INVALID)

        if coupon.kind == Coupon.Kind.FIXED and coupon.currency:
            plan_currency = (plan.currency or "").upper()
            if plan_currency and plan_currency != coupon.currency:
                # No exchange rates in this application, and inventing one at
                # checkout would be the worst possible place to start.
                logger.info(
                    "Coupon %s refused for tenant %s: %s discount against a %s plan.",
                    coupon.code, tenant_id, coupon.currency, plan_currency,
                )
                raise CouponError(INVALID)

    if coupon.max_redemptions and _redemptions_used(coupon) >= coupon.max_redemptions:
        logger.info("Coupon %s refused for tenant %s: fully redeemed.", coupon.code, tenant_id)
        raise CouponError(INVALID)

    if coupon.max_per_tenant and _redemptions_used(coupon, tenant_id) >= coupon.max_per_tenant:
        logger.info("Coupon %s refused for tenant %s: already used by this workspace.", coupon.code, tenant_id)
        raise CouponError(INVALID)


def quote(*, tenant, code: str, plan, now=None) -> dict:
    """
    What a code is worth against a plan, without taking a slot.

    This is what the upgrade screen calls while somebody is typing. It writes
    nothing, so a customer trying three codes to see which is best does not
    burn three redemptions -- and it returns the same numbers `reserve` will,
    computed the same way, so the price shown is the price charged.
    """
    coupon = resolve(tenant, code)
    check(coupon, tenant=tenant, plan=plan, now=now)

    list_amount = int(plan.price_cents)
    if coupon.kind == Coupon.Kind.TRIAL_EXTENSION:
        # A trial extension is not a discount and must not be shown as one.
        # Returning a zero discount alongside the days is what lets the UI say
        # "14 extra days" rather than "₹0 off".
        return {
            "code": coupon.code,
            "kind": coupon.kind,
            "description": coupon.description,
            "list_amount_minor": list_amount,
            "discount_minor": 0,
            "amount_minor": list_amount,
            "trial_days": int(coupon.value),
            "currency": (plan.currency or "").upper(),
        }

    discount = coupon.discount_for(list_amount)
    return {
        "code": coupon.code,
        "kind": coupon.kind,
        "description": coupon.description,
        "list_amount_minor": list_amount,
        "discount_minor": discount,
        "amount_minor": max(list_amount - discount, 0),
        "trial_days": 0,
        "currency": (plan.currency or "").upper(),
    }


def reserve(*, tenant, coupon, checkout, discount_minor: int) -> CouponRedemption:
    """
    Take a slot for this checkout. Call inside the checkout's transaction.

    The `select_for_update` is on the **coupon** rather than on the redemption
    rows, because the thing being protected is a count and there is no row yet
    to lock. Two customers racing for the last slot of a coupon serialise on
    that row, and the second one re-counts and is refused -- which is exactly
    what a limit of one is supposed to mean.
    """
    locked = Coupon.objects.select_for_update().get(pk=coupon.pk)

    # A retry is not a second redemption. Looked for **before** the limit
    # check, because the first reservation already occupies a slot: with a
    # per-tenant limit of one, re-running this for the same checkout would
    # otherwise be refused by the reservation it is trying to return.
    existing = CouponRedemption.objects.filter(coupon=locked, checkout=checkout).first()
    if existing is not None:
        return existing

    check(locked, tenant=tenant, plan=checkout.plan)

    try:
        with transaction.atomic():
            return CouponRedemption.objects.create(
                coupon=locked,
                tenant=checkout.tenant,
                checkout=checkout,
                discount_minor=int(discount_minor),
                status=CouponRedemption.Status.RESERVED,
            )
    except IntegrityError:
        # Two requests for the same checkout raced past the read above. The
        # unique constraint let one through; the loser takes the winner's row,
        # which is the correct outcome rather than an error.
        existing = CouponRedemption.objects.filter(coupon=locked, checkout=checkout).first()
        if existing is not None:
            return existing
        raise


def confirm(checkout, *, now=None) -> int:
    """
    A payment captured: turn this checkout's reservations into redemptions.

    Idempotent by filtering on RESERVED, so the webhook and the browser
    callback both arriving -- which happens on nearly every payment -- settles
    once. Returns how many rows moved, for the log.
    """
    return CouponRedemption.objects.filter(
        checkout=checkout, status=CouponRedemption.Status.RESERVED,
    ).update(status=CouponRedemption.Status.REDEEMED, settled_at=now or timezone.now())


def release(checkout, *, now=None) -> int:
    """
    A checkout died: give its slots back.

    Only RESERVED rows move. A REDEEMED one belongs to a payment that
    happened, and releasing it would let a paying customer's coupon be used
    again -- so a failure arriving after a capture (a late webhook for an
    earlier failed attempt) cannot undo the redemption.
    """
    return CouponRedemption.objects.filter(
        checkout=checkout, status=CouponRedemption.Status.RESERVED,
    ).update(status=CouponRedemption.Status.RELEASED, settled_at=now or timezone.now())


@transaction.atomic
def redeem_trial_extension(*, tenant, code: str, now=None) -> dict:
    """
    Add days to a running trial, once.

    Separate from checkout because there is no order and no money: this moves
    `trial_ends_at` and nothing else. Everything about *access* still derives
    from that date -- see the long note in `Subscription.access_state` -- so
    extending the column is the whole of extending the trial, with no second
    place for the two to disagree.

    Refuses on anything but a live trial. Handing extra trial days to a paying
    customer would silently move them onto a free tier, and handing them to an
    expired one would resurrect access somebody has already decided to end.
    """
    from apps.subscriptions.models import Subscription

    now = now or timezone.now()
    coupon = resolve(tenant, code)

    if coupon.kind != Coupon.Kind.TRIAL_EXTENSION:
        raise CouponError("That code is a discount. Apply it when you choose a plan.")

    locked = Coupon.objects.select_for_update().get(pk=coupon.pk)
    check(locked, tenant=tenant, plan=None, now=now)

    subscription = (
        Subscription.objects.select_for_update()
        .filter(tenant_id=getattr(tenant, "pk", tenant))
        .first()
    )
    if subscription is None or not subscription.is_trial:
        raise CouponError("This code extends a free trial, and this workspace is not on one.")
    if subscription.trial_ends_at is not None and subscription.trial_ends_at <= now:
        raise CouponError("This trial has already ended. Choose a plan to keep sending.")

    from datetime import timedelta

    days = int(locked.value)
    # Extended from the existing end date rather than from now, so a customer
    # who redeems on day one of a fourteen-day trial gets 14 + n days, not n.
    subscription.trial_ends_at = (subscription.trial_ends_at or now) + timedelta(days=days)
    subscription.save(update_fields=["trial_ends_at", "updated_at"])

    CouponRedemption.objects.create(
        coupon=locked,
        tenant_id=getattr(tenant, "pk", tenant),
        checkout=None,
        discount_minor=0,
        trial_days=days,
        # Nothing further has to happen for this to be real, so it is redeemed
        # immediately rather than reserved.
        status=CouponRedemption.Status.REDEEMED,
        settled_at=now,
    )

    logger.info(
        "Tenant %s extended its trial by %s day(s) with coupon %s.",
        getattr(tenant, "pk", tenant), days, locked.code,
    )
    return {
        "code": locked.code,
        "trial_days": days,
        "trial_ends_at": subscription.trial_ends_at,
    }
