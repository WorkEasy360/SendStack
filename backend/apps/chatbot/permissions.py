"""
Who may do what with a chatbot.

**This is not a second RBAC system.** The platform already has two facts about
a person -- ``accounts.User.role`` says what they may administer, and
``teams.TeamMember.role`` says whether they are a manager or an agent -- and
this file maps the chatbot's vocabulary of permissions onto those. Inventing a
third role model would mean two places to grant access and one of them
eventually forgotten.

The mapping, in one sentence: **an agent works conversations, a manager
configures the bot** -- but only once somebody has actually been made a
manager.

That qualifier is the whole subtlety. ``TeamMember.role`` defaults to AGENT and
the platform assigns it on its own: ``teams.services.ensure_member`` creates a
row the first time anyone opens the inbox, and the team list endpoint
bulk-creates one for every active user in the tenant. Everybody is therefore an
AGENT by default, workspace owner included, and treating that as "may not
configure the bot" locks the owner out of their own chatbot as a side effect of
visiting the Team page. So an AGENT row restricts only when the roster also
holds a MANAGER, which is the one thing in it that can only have come from a
decision. Until then it is all defaults and carries no information, and
everyone gets everything.

Tenant isolation is *not* implemented here. It is applied in every queryset, in
the query rather than as a check afterwards, so another workspace's id is a 404
and never a 403 -- a 403 would confirm the object exists. See
``views.TenantScopedViewSet``.
"""

from rest_framework.permissions import BasePermission

#: The vocabulary, as the specification names it. Kept as constants so a view
#: declares `required_permission = Permission.CHATBOT_EDIT` and a reader can
#: match it against the documentation without translating.
class Permission:
    CHATBOT_VIEW = "CHATBOT_VIEW"
    CHATBOT_CREATE = "CHATBOT_CREATE"
    CHATBOT_EDIT = "CHATBOT_EDIT"
    CHATBOT_DELETE = "CHATBOT_DELETE"
    CHATBOT_PUBLISH = "CHATBOT_PUBLISH"
    CHATBOT_ANALYTICS = "CHATBOT_ANALYTICS"
    CONVERSATION_VIEW = "CONVERSATION_VIEW"
    CONVERSATION_REPLY = "CONVERSATION_REPLY"
    CONVERSATION_ASSIGN = "CONVERSATION_ASSIGN"
    CONVERSATION_TAKEOVER = "CONVERSATION_TAKEOVER"


#: What an agent may do. Everything about *conversations* and nothing about
#: *configuration*: an agent answering customers all day needs the inbox, the
#: takeover button and the numbers, and has no business republishing the bot
#: mid-shift.
AGENT_PERMISSIONS = frozenset({
    Permission.CHATBOT_VIEW,
    Permission.CHATBOT_ANALYTICS,
    Permission.CONVERSATION_VIEW,
    Permission.CONVERSATION_REPLY,
    Permission.CONVERSATION_ASSIGN,
    Permission.CONVERSATION_TAKEOVER,
})

#: Everything.
MANAGER_PERMISSIONS = frozenset({
    value for name, value in vars(Permission).items() if not name.startswith("_") and isinstance(value, str)
})


def _roster_is_curated(tenant_id) -> bool:
    """
    Has anybody in this workspace actually been made a manager?

    Asked because ``TeamMember.role`` defaults to AGENT for everyone, so the
    presence of a MANAGER is the only evidence that the roster reflects a
    decision rather than a default. Cheap, indexed, and only reached for a user
    whose own row says AGENT.
    """
    from apps.teams.models import TeamMember

    return TeamMember.objects.filter(tenant_id=tenant_id, role=TeamMember.Role.MANAGER).exists()


def permissions_for(user) -> frozenset:
    """
    Everything this user may do with chatbots.

    One query, and only when the user is actually on the team roster. The
    result is cached on the request's user object for the life of the request,
    because a list endpoint's permission check and its serializer both ask.
    """
    if user is None or not user.is_authenticated or not user.tenant_id:
        return frozenset()

    cached = getattr(user, "_chatbot_permissions", None)
    if cached is not None:
        return cached

    from apps.accounts.models import User as UserModel

    if user.role in (UserModel.Role.SUPERADMIN, UserModel.Role.RESELLER):
        granted = MANAGER_PERMISSIONS
    else:
        from apps.teams.models import TeamMember

        role = (
            TeamMember.objects
            .filter(user=user, tenant_id=user.tenant_id)
            .values_list("role", flat=True)
            .first()
        )
        # AGENT restricts only once the roster says somebody else runs the
        # place. On its own it means nothing: it is the model default, and the
        # platform hands it out by itself -- `teams.services.ensure_member`
        # creates a row the first time anyone opens the inbox, and the team
        # list endpoint bulk-creates one for *every* active user in the tenant.
        # So the workspace owner ends up an AGENT by bookkeeping rather than by
        # anyone's decision, and reading that as "may not configure the bot"
        # locked them out of their own chatbot the day they visited the Team
        # page. A permission system whose default state shuts out the person
        # who grants permissions is broken, so an uncurated roster grants
        # everything and only a deliberate one takes anything away.
        if role == TeamMember.Role.AGENT and _roster_is_curated(user.tenant_id):
            granted = AGENT_PERMISSIONS
        else:
            granted = MANAGER_PERMISSIONS

    user._chatbot_permissions = granted
    return granted


def has_permission(user, permission) -> bool:
    return permission in permissions_for(user)


class HasChatbotPermission(BasePermission):
    """
    Checks the permission a view declares.

    A view that declares **nothing** is refused rather than allowed. Forgetting
    the attribute must not publish an endpoint -- the same default-deny rule the
    public API's scopes use, and for the same reason.
    """

    message = "Your role does not allow this."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        required = getattr(view, "required_permissions", None)
        if required is None:
            required = getattr(view, "required_permission", None)
            required = [required] if required else None
        if not required:
            return False

        # A per-action override, so one viewset can require CHATBOT_VIEW to
        # list and CHATBOT_EDIT to write without being split into two.
        action = getattr(view, "action", None)
        overrides = getattr(view, "action_permissions", {}) or {}
        if action in overrides:
            required = overrides[action]
            required = [required] if isinstance(required, str) else required

        granted = permissions_for(request.user)
        return all(permission in granted for permission in required)
