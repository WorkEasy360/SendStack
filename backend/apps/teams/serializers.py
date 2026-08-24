from rest_framework import serializers

from .models import AssignmentPolicy, CannedReply, TeamMember, WorkingHours


class TeamMemberSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    open_conversations = serializers.IntegerField(read_only=True)

    class Meta:
        model = TeamMember
        fields = (
            "id", "user", "username", "email", "role", "is_available", "auto_assign",
            "max_open_conversations", "open_conversations", "created_at", "updated_at",
        )
        read_only_fields = ("id", "user", "username", "email", "open_conversations", "created_at", "updated_at")


class AssignmentPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = AssignmentPolicy
        fields = (
            "mode", "reassign_when_unassigned", "respect_availability",
            "first_response_target_minutes", "resolution_target_minutes",
            "working_hours_enabled", "send_away_message", "away_message",
        )

    def validate(self, attrs):
        merged = {**{field: getattr(self.instance, field, None) for field in self.fields}, **attrs}
        if merged.get("send_away_message") and not (merged.get("away_message") or "").strip():
            raise serializers.ValidationError(
                {"away_message": "Write the message to send before turning the away reply on."}
            )
        if merged.get("send_away_message") and not merged.get("working_hours_enabled"):
            # Without working hours the team is open by definition, so the
            # away message would never fire and the toggle would look broken.
            raise serializers.ValidationError(
                {"send_away_message": "Set your working hours first -- otherwise the team is never closed."}
            )
        return attrs


class WorkingHoursSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkingHours
        fields = ("id", "weekday", "is_open", "opens_at", "closes_at")
        read_only_fields = ("id",)


class CannedReplySerializer(serializers.ModelSerializer):
    class Meta:
        model = CannedReply
        fields = ("id", "shortcut", "title", "body", "usage_count", "created_by", "created_at", "updated_at")
        read_only_fields = ("id", "usage_count", "created_by", "created_at", "updated_at")

    def validate_shortcut(self, value):
        cleaned = value.strip().lstrip("/").lower()
        if not cleaned:
            raise serializers.ValidationError("A shortcut is required.")
        if any(character.isspace() for character in cleaned):
            # The composer matches everything after "/" up to a space, so a
            # shortcut containing one could never be typed.
            raise serializers.ValidationError("A shortcut cannot contain spaces.")
        return cleaned
