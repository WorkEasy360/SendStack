from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer


class MultiSenderTokenSerializer(TokenObtainPairSerializer):
    """JWT pair with non-sensitive context needed by the frontend."""

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["tenant_id"] = user.tenant_id
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = CurrentUserSerializer(self.user).data
        return data


class CurrentUserSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    username = serializers.CharField(read_only=True)
    email = serializers.EmailField(read_only=True)
    role = serializers.CharField(read_only=True)
    tenant_id = serializers.IntegerField(read_only=True)
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    credits = serializers.IntegerField(read_only=True)
    reserved_credits = serializers.IntegerField(read_only=True)
    available_credits = serializers.SerializerMethodField()

    def get_available_credits(self, obj):
        return obj.credits - obj.reserved_credits


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, min_length=12, trim_whitespace=False)

    def validate_new_password(self, value):
        from django.contrib.auth.password_validation import validate_password

        validate_password(value)
        return value


class ManagedUserSerializer(serializers.ModelSerializer):
    tenant_id = serializers.IntegerField(read_only=True)
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    available_credits = serializers.SerializerMethodField()

    class Meta:
        from .models import User

        model = User
        fields = (
            "id", "username", "email", "first_name", "last_name", "mobile", "role", "tenant_id", "tenant_name",
            "is_active", "credits", "reserved_credits", "available_credits", "created_at",
        )
        read_only_fields = ("id", "role", "tenant_id", "tenant_name", "credits", "reserved_credits", "created_at")

    def get_available_credits(self, obj):
        return obj.credits - obj.reserved_credits


class ManagedUserCreateSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=12, trim_whitespace=False)
    role = serializers.ChoiceField(choices=[("RESELLER", "Reseller"), ("USER", "User")])
    tenant_name = serializers.CharField(max_length=150)
    mobile = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate_username(self, value):
        from .models import User

        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("A user with this username already exists.")
        return value

    def validate_password(self, value):
        from django.contrib.auth.password_validation import validate_password

        validate_password(value)
        return value


class ImpersonationStartSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(max_length=500, min_length=3)
