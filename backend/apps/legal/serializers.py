from rest_framework import serializers

from .models import LegalAcceptance


class LegalAcceptanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = LegalAcceptance
        # The IP address and user agent are stored as evidence, not shown back:
        # nothing in the UI needs them, and echoing them would spread a copy of
        # somebody's address into every client that reads this endpoint.
        fields = ("id", "document", "version", "accepted_at")
        read_only_fields = fields
