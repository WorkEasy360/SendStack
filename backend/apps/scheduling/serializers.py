from rest_framework import serializers

from .models import ScheduledJob


class ScheduledJobSerializer(serializers.ModelSerializer):
    """
    What a customer may see about their own scheduled work.

    `locked_by` is deliberately absent: it names a host and a process id,
    which is infrastructure detail and no use to the person whose campaign it
    is. `last_error` is included because it is written for them -- see
    JobPrecondition, whose messages are sentences, not tracebacks.
    """

    class Meta:
        model = ScheduledJob
        fields = (
            "id", "kind", "object_id", "scheduled_for", "status",
            "attempts", "max_attempts", "executed_at", "last_error", "created_at",
        )
        read_only_fields = fields
