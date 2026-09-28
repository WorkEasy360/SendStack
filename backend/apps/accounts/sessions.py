"""
Ending every session a user has, not just the one in front of them.

A password change is the moment somebody says "I think somebody else has my
account". If the refresh tokens minted before that moment stay valid, the
change accomplishes very little: refresh tokens live seven days here
(SIMPLE_JWT.REFRESH_TOKEN_LIFETIME), so a token copied off a stolen laptop
keeps producing fresh 15-minute access tokens for a week after the victim has
"secured" the account.

SimpleJWT already records every refresh token it issues in `OutstandingToken`
(that is what the token_blacklist app is for, and it is installed). Blacklisting
the outstanding rows for one user is therefore a complete answer for refresh
tokens, and the honest limitation is access tokens: those are signed, stateless
and checked without a database round trip, so one already in an attacker's hands
stays valid until it expires. Fifteen minutes is the whole exposure, and closing
it would mean a blacklist lookup on every single API call.
"""

import logging

logger = logging.getLogger("sendstack")


def revoke_all_refresh_tokens(user) -> int:
    """
    Blacklist every refresh token outstanding for this user. Returns how many.

    Best-effort: a password change that has already been written must not be
    reported as failed because the blacklist write did not land. It is logged
    at error level precisely because it is the kind of quiet failure that
    leaves a session alive after somebody was told it was closed.
    """
    try:
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
    except ImportError:  # pragma: no cover - the app is in INSTALLED_APPS
        logger.error("Token blacklist app is unavailable; sessions were NOT revoked.")
        return 0

    revoked = 0
    try:
        for token in OutstandingToken.objects.filter(user=user).iterator():
            # get_or_create rather than create: a token blacklisted by an
            # earlier logout is already dead and must not raise here.
            _, created = BlacklistedToken.objects.get_or_create(token=token)
            if created:
                revoked += 1
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("Could not revoke every session for user id=%s.", getattr(user, "pk", "?"))

    logger.info("Revoked %s refresh token(s) for user id=%s.", revoked, getattr(user, "pk", "?"))
    return revoked
