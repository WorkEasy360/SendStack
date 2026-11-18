"""
The refusal codes SendStack's own sign-in rules add to the connector's
vocabulary, and what the sign-in page says for each.

No Django imports: settings reads this to register the codes with the
connector (WORKEASY360_SSO["ERROR_MESSAGES"]) before the apps are loaded.
The single-page app mirrors them in frontend/src/pages/auth/workeasy360.js.
"""

ERROR_MESSAGES = {
    "other_organization": (
        "Your SendStack account belongs to another WorkEasy360 organization. "
        "Sign in to that organization, or ask an administrator."
    ),
    "role_not_allowed": (
        "This SendStack account is a reseller or platform account and cannot be used "
        "through WorkEasy360. Sign in with its password instead."
    ),
    "no_workspace": (
        "Your organization has no SendStack workspace yet. Ask your SendStack administrator to link it."
    ),
    "workspace_suspended": "Your organization's SendStack workspace is suspended. Contact support.",
    "seat_limit": "Your workspace has no room for another user on its current plan.",
    "access_unverified": (
        "SendStack cannot confirm your access with WorkEasy360 right now. Try again in a few minutes."
    ),
}
