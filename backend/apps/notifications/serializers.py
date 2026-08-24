from rest_framework import serializers

from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    actor_username = serializers.CharField(source="actor.username", read_only=True, default=None)

    class Meta:
        model = Notification
        fields = ("id", "kind", "title", "body", "link", "actor", "actor_username", "read_at", "created_at")
        read_only_fields = fields
