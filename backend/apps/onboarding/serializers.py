from rest_framework import serializers

from .models import OnboardingState


class OnboardingStateSerializer(serializers.ModelSerializer):
    """
    The checklist as the frontend renders it.

    `steps` and `next_step` are computed from the same milestone columns the
    activation metric uses, so there is one answer to "what is done" and the
    UI cannot draw a different picture from the one the backend believes.
    """

    steps = serializers.SerializerMethodField()
    next_step = serializers.SerializerMethodField()
    is_complete = serializers.SerializerMethodField()

    class Meta:
        model = OnboardingState
        fields = (
            "is_guided", "dismissed_at", "activated_at",
            "steps", "next_step", "is_complete",
        )
        read_only_fields = fields

    def get_steps(self, obj):
        return obj.as_steps()

    def get_next_step(self, obj):
        return obj.next_step()

    def get_is_complete(self, obj):
        return obj.next_step() is None
