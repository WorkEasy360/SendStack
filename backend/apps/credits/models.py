from django.conf import settings
from django.db import models


class CreditTransaction(models.Model):
    """
    One immutable row per account whose balance moved.

    A transfer between two accounts is recorded as two rows (one per
    account) sharing the same `type`/`amount`/`reference`, each carrying its
    own `account`'s balance_before/after -- this is the standard double-entry
    pattern and avoids ambiguity about whose balance a single row describes.
    """

    class Type(models.TextChoices):
        GRANT = "GRANT", "Grant"          # platform-minted, no debit anywhere
        TRANSFER = "TRANSFER", "Transfer"  # reseller allocating from their own balance
        RESERVE = "RESERVE", "Reserve"     # held against a launching campaign
        RELEASE = "RELEASE", "Release"     # unused reservation returned
        CONSUME = "CONSUME", "Consume"     # spent on a successfully sent message
        REFUND = "REFUND", "Refund"

    account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="credit_transactions")
    from_account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    to_account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    amount = models.PositiveIntegerField()
    type = models.CharField(max_length=20, choices=Type.choices)
    reference = models.CharField(max_length=255, blank=True)
    balance_before = models.PositiveIntegerField()
    balance_after = models.PositiveIntegerField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["account", "-created_at"])]

    def __str__(self):
        return f"{self.type} {self.amount} on account #{self.account_id}"
