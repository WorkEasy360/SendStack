from rest_framework.permissions import BasePermission


class IsSuperadmin(BasePermission):
    message = "Superadmin access is required."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role == request.user.Role.SUPERADMIN
        )


class CanManageTargetUser(BasePermission):
    message = "You cannot manage this user."

    def has_object_permission(self, request, view, obj):
        return request.user.can_manage(obj)
