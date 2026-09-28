from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer


class SendStackTokenSerializer(TokenObtainPairSerializer):
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
    # Optional so existing API callers keep working, but checked when it is
    # sent -- which the reset form always does. A mistyped new password on a
    # single-field form locks somebody out of the account they were in the
    # middle of recovering.
    confirm_password = serializers.CharField(
        write_only=True, required=False, allow_blank=True, trim_whitespace=False,
    )

    def validate_new_password(self, value):
        from django.contrib.auth.password_validation import validate_password

        validate_password(value)
        return value

    def validate(self, attrs):
        confirmation = attrs.get("confirm_password")
        if confirmation and confirmation != attrs["new_password"]:
            raise serializers.ValidationError({"confirm_password": "The two passwords do not match."})
        return attrs


class PasswordChangeSerializer(serializers.Serializer):
    """Changing your own password: prove the old one, then meet the same rules as a reset."""

    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, min_length=12, trim_whitespace=False)

    def validate_current_password(self, value):
        user = self.context["user"]
        if not user.check_password(value):
            raise serializers.ValidationError("That is not your current password.")
        return value

    def validate_new_password(self, value):
        from django.contrib.auth.password_validation import validate_password

        # Passed the user, so UserAttributeSimilarityValidator can actually do
        # its job -- without it, "myusername1234" passes every other rule.
        validate_password(value, user=self.context["user"])
        return value

    def validate(self, attrs):
        if attrs["current_password"] == attrs["new_password"]:
            raise serializers.ValidationError({"new_password": "Choose a password you have not used here."})
        return attrs


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


class SignupSerializer(serializers.Serializer):
    """
    What a stranger may send to create an account. Note what is absent.

    There is no `tenant_id`, no `reseller_id`, no `owner_id`, no `role`, no
    `price` and no `amount` -- not validated-and-rejected, *absent*, so there
    is nothing for a caller to guess at and nothing for a future edit to
    accidentally start honouring. `plan` is the sole exception and it is only
    a name: the server resolves it against its own public plans and takes the
    price and the limits from the row it finds (see
    apps.accounts.registration.resolve_requested_plan).
    """

    full_name = serializers.CharField(max_length=150, trim_whitespace=True)
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, min_length=12, trim_whitespace=False)
    company = serializers.CharField(max_length=150, trim_whitespace=True)

    # The plan they clicked on the pricing page, if any. A preference, not an
    # instruction: an unknown or non-public name simply falls back to the
    # deployment's trial plan rather than failing the signup.
    plan = serializers.CharField(max_length=80, required=False, allow_blank=True)

    # Affirmative action, and the serializer will not accept anything else:
    # `False` is a validation error rather than a quiet default. A signup form
    # that ships this pre-ticked, or that omits it, cannot create an account.
    accept_terms = serializers.BooleanField()

    # Defaults to False and is a *separate* question from accept_terms. See
    # the field comment on User.marketing_opt_in.
    marketing_opt_in = serializers.BooleanField(required=False, default=False)

    def validate_accept_terms(self, value):
        if not value:
            raise serializers.ValidationError(
                "You must accept the Terms of Service and Privacy Policy to create an account."
            )
        return value

    def validate_email(self, value):
        from .registration import normalize_email

        return normalize_email(value)

    def validate_company(self, value):
        if not value.strip():
            raise serializers.ValidationError("Tell us your business or workspace name.")
        return value.strip()

    def validate_password(self, value):
        from django.contrib.auth.password_validation import validate_password

        # Run here as well as in `register`, so the browser gets field-level
        # feedback ("this password is too common") rather than one flat
        # sentence -- the service-layer check is the one that is authoritative
        # and it stays, because a service must never rely on its caller.
        validate_password(value)
        return value
