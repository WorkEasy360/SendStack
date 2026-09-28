from django.contrib.auth import get_user_model
from django.db import models, transaction

from .models import CreditTransaction

User = get_user_model()


class CreditError(Exception):
    """Base class for credit operations that were refused."""


class InsufficientCreditsError(CreditError):
    pass


def visible_accounts(actor):
    """Accounts whose ledger `actor` is allowed to see, mirroring ManagedUserViewSet's scoping."""
    if actor.role == actor.Role.SUPERADMIN:
        return User.objects.all()
    if actor.role == actor.Role.RESELLER and actor.tenant_id:
        return User.objects.filter(models.Q(pk=actor.pk) | models.Q(role=actor.Role.USER, tenant__parent_id=actor.tenant_id))
    return User.objects.filter(pk=actor.pk)


def _record(*, account, from_account, to_account, amount, type_, balance_before, balance_after, reference,
            created_by, operation_id=None):
    return CreditTransaction.objects.create(
        account=account, from_account=from_account, to_account=to_account, amount=amount, type=type_,
        balance_before=balance_before, balance_after=balance_after, reference=reference, created_by=created_by,
        operation_id=operation_id or None,
    )


def operation_applied(operation_id: str) -> bool:
    """Whether a balance move with this idempotency handle has already been recorded."""
    if not operation_id:
        return False
    return CreditTransaction.objects.filter(operation_id=operation_id).exists()


def assign_credits(*, actor, target, amount: int, reference: str = "", deduct: bool = True):
    """
    Superadmins always mint new credits (no debit anywhere).

    A reseller assigning to their own user normally transfers out of their
    own balance (`deduct=True`, the default). Passing `deduct=False` lets
    the reseller top up their user without touching their own balance
    instead -- a plain mint, same as the superadmin path.
    """
    if amount <= 0:
        raise CreditError("Amount must be positive.")
    if not actor.can_manage(target) or target.pk == actor.pk:
        raise CreditError("You cannot assign credits to this account.")

    with transaction.atomic():
        if actor.role == actor.Role.SUPERADMIN or not deduct:
            locked_target = User.objects.select_for_update().get(pk=target.pk)
            before = locked_target.credits
            locked_target.credits = before + amount
            locked_target.save(update_fields=["credits"])
            credit_txn = _record(
                account=locked_target, from_account=None, to_account=locked_target, amount=amount,
                type_=CreditTransaction.Type.GRANT, balance_before=before, balance_after=locked_target.credits,
                reference=reference, created_by=actor,
            )
            return None, credit_txn

        # Reseller (the only other role that can_manage() allows through): transfer from their own balance.
        first_pk, second_pk = sorted((actor.pk, target.pk))
        locked_first = User.objects.select_for_update().get(pk=first_pk)
        locked_second = User.objects.select_for_update().get(pk=second_pk)
        locked_actor = locked_first if locked_first.pk == actor.pk else locked_second
        locked_target = locked_first if locked_first.pk == target.pk else locked_second

        available = locked_actor.credits - locked_actor.reserved_credits
        if available < amount:
            raise InsufficientCreditsError(f"Only {available} credits are available to assign.")

        actor_before = locked_actor.credits
        locked_actor.credits = actor_before - amount
        locked_actor.save(update_fields=["credits"])
        debit_txn = _record(
            account=locked_actor, from_account=locked_actor, to_account=locked_target, amount=amount,
            type_=CreditTransaction.Type.TRANSFER, balance_before=actor_before, balance_after=locked_actor.credits,
            reference=reference, created_by=actor,
        )

        target_before = locked_target.credits
        locked_target.credits = target_before + amount
        locked_target.save(update_fields=["credits"])
        credit_txn = _record(
            account=locked_target, from_account=locked_actor, to_account=locked_target, amount=amount,
            type_=CreditTransaction.Type.TRANSFER, balance_before=target_before, balance_after=locked_target.credits,
            reference=reference, created_by=actor,
        )

        return debit_txn, credit_txn


def deduct_credits(*, actor, target, amount: int, reference: str = ""):
    """
    Claw back credits from an account one level below `actor`, without
    deleting it. The deducted amount flows back into `actor`'s own balance
    (credit never simply vanishes -- same principle as
    `reclaim_credits_on_delete`), and can never dip into credits already
    reserved against an in-progress campaign.
    """
    if amount <= 0:
        raise CreditError("Amount must be positive.")
    if not actor.can_manage(target) or target.pk == actor.pk:
        raise CreditError("You cannot deduct credits from this account.")
    if target.role == target.Role.SUPERADMIN:
        raise CreditError("Cannot deduct credits from a super admin account.")

    with transaction.atomic():
        first_pk, second_pk = sorted((actor.pk, target.pk))
        locked_first = User.objects.select_for_update().get(pk=first_pk)
        locked_second = User.objects.select_for_update().get(pk=second_pk)
        locked_actor = locked_first if locked_first.pk == actor.pk else locked_second
        locked_target = locked_first if locked_first.pk == target.pk else locked_second

        available = locked_target.credits - locked_target.reserved_credits
        if available < amount:
            raise InsufficientCreditsError(f"Only {available} credits are available to deduct.")

        target_before = locked_target.credits
        locked_target.credits = target_before - amount
        locked_target.save(update_fields=["credits"])
        debit_txn = _record(
            account=locked_target, from_account=locked_target, to_account=locked_actor, amount=amount,
            type_=CreditTransaction.Type.REFUND, balance_before=target_before, balance_after=locked_target.credits,
            reference=reference, created_by=actor,
        )

        actor_before = locked_actor.credits
        locked_actor.credits = actor_before + amount
        locked_actor.save(update_fields=["credits"])
        credit_txn = _record(
            account=locked_actor, from_account=locked_target, to_account=locked_actor, amount=amount,
            type_=CreditTransaction.Type.REFUND, balance_before=actor_before, balance_after=locked_actor.credits,
            reference=reference, created_by=actor,
        )

        return debit_txn, credit_txn


def reclaim_credits_on_delete(*, target, actor):
    """
    Return a deleted account's unspent balance to the account one level up
    the hierarchy (a deleted reseller's credits go to the platform
    superadmin that minted them; a deleted user's credits go back to the
    reseller who allocated them), so credit never simply vanishes.
    """
    if target.role == target.Role.SUPERADMIN:
        raise CreditError("Super admin accounts cannot be deleted.")
    if target.reserved_credits > 0:
        raise CreditError("This account has credits reserved by an in-progress campaign. Cancel it first.")
    if target.credits <= 0:
        return None

    parent_role = target.Role.SUPERADMIN if target.role == target.Role.RESELLER else target.Role.RESELLER
    parent_tenant_id = target.tenant.parent_id
    if not parent_tenant_id:
        raise CreditError("No parent account was found to return these credits to.")

    with transaction.atomic():
        parent = User.objects.select_for_update().filter(
            tenant_id=parent_tenant_id, role=parent_role, is_active=True, is_deleted=False
        ).order_by("id").first()
        if not parent:
            raise CreditError("No parent account was found to return these credits to.")

        locked_target = User.objects.select_for_update().get(pk=target.pk)
        amount = locked_target.credits
        if amount <= 0:
            return None

        target_before = locked_target.credits
        locked_target.credits = 0
        locked_target.save(update_fields=["credits"])
        _record(
            account=locked_target, from_account=locked_target, to_account=parent, amount=amount,
            type_=CreditTransaction.Type.REFUND, balance_before=target_before, balance_after=0,
            reference=f"Account '{target.username}' deleted by {actor.username}", created_by=actor,
        )

        parent_before = parent.credits
        parent.credits = parent_before + amount
        parent.save(update_fields=["credits"])
        _record(
            account=parent, from_account=locked_target, to_account=parent, amount=amount,
            type_=CreditTransaction.Type.REFUND, balance_before=parent_before, balance_after=parent.credits,
            reference=f"Reclaimed from deleted account '{target.username}'", created_by=actor,
        )

        return amount


def reserve_credits(*, user_id: int, amount: int, reference: str = "") -> None:
    if amount <= 0:
        return
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=user_id)
        available = locked.credits - locked.reserved_credits
        if available < amount:
            raise InsufficientCreditsError(f"This requires {amount} credits but only {available} are available.")
        locked.reserved_credits += amount
        locked.save(update_fields=["reserved_credits"])
        _record(
            account=locked, from_account=None, to_account=None, amount=amount,
            type_=CreditTransaction.Type.RESERVE, balance_before=locked.credits, balance_after=locked.credits,
            reference=reference, created_by=None,
        )


def release_credits(*, user_id: int, amount: int = 1, reference: str = "", operation_id: str = "") -> None:
    """
    Return a held reservation.

    `operation_id`, when given, makes this fire at most once ever: a second
    call with the same handle (a redelivered task, the recovery sweep cleaning
    up after a dead worker, a duplicate webhook) is a no-op. The check runs
    *under the account row lock* so two racing callers serialise -- the second
    sees the first's row and returns.
    """
    if amount <= 0:
        return
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=user_id)
        if operation_id and operation_applied(operation_id):
            return
        to_release = min(amount, locked.reserved_credits)
        if to_release == 0 and not operation_id:
            return
        locked.reserved_credits -= to_release
        locked.save(update_fields=["reserved_credits"])
        _record(
            account=locked, from_account=None, to_account=None, amount=to_release,
            type_=CreditTransaction.Type.RELEASE, balance_before=locked.credits, balance_after=locked.credits,
            reference=reference, created_by=None, operation_id=operation_id,
        )


def consume_credit(*, user_id: int, amount: int = 1, reference: str = "", operation_id: str = "") -> None:
    """
    Spend a reserved credit on a message that was actually sent.

    `operation_id` gives the same fire-once guarantee as `release_credits`: the
    send path keys it on the message, so a message is charged exactly once
    however many times the task runs or the sweep reconciles it.
    """
    if amount <= 0:
        return
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=user_id)
        if operation_id and operation_applied(operation_id):
            return
        locked.reserved_credits -= min(amount, locked.reserved_credits)
        before = locked.credits
        locked.credits = max(0, before - amount)
        locked.save(update_fields=["credits", "reserved_credits"])
        _record(
            account=locked, from_account=locked, to_account=None, amount=amount,
            type_=CreditTransaction.Type.CONSUME, balance_before=before, balance_after=locked.credits,
            reference=reference, created_by=None, operation_id=operation_id,
        )
