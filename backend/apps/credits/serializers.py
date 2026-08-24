from rest_framework import serializers

from .models import CreditTransaction


class CreditTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = CreditTransaction
        fields = (
            "id", "account", "from_account", "to_account", "amount", "type",
            "reference", "balance_before", "balance_after", "created_by", "created_at",
        )
        read_only_fields = fields


class AssignCreditsSerializer(serializers.Serializer):
    target_user_id = serializers.IntegerField(min_value=1)
    amount = serializers.IntegerField(min_value=1)
    reference = serializers.CharField(max_length=255, required=False, allow_blank=True)
    deduct = serializers.BooleanField(required=False, default=True)


class DeductCreditsSerializer(serializers.Serializer):
    target_user_id = serializers.IntegerField(min_value=1)
    amount = serializers.IntegerField(min_value=1)
    reference = serializers.CharField(max_length=255, required=False, allow_blank=True)
