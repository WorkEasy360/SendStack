"""Recording and querying legal acceptances."""

import logging

from .documents_registry import ACCEPTANCE_REQUIRED, DOCUMENTS, current_version
from .models import LegalAcceptance

logger = logging.getLogger("sendstack")


def outstanding_for(user) -> list:
    """
    The documents this user has not accepted at their current version.

    One query for every document rather than one per document: this runs on
    every load of the application shell, and three round trips to answer
    "nothing to do" is three too many.
    """
    if user is None or not getattr(user, "pk", None):
        return []

    wanted = {slug: current_version(slug) for slug in ACCEPTANCE_REQUIRED}
    accepted = set(
        LegalAcceptance.objects.filter(user=user, document__in=wanted)
        .values_list("document", "version")
    )
    return [
        {"slug": slug, "title": DOCUMENTS[slug][0], "version": version}
        for slug, version in wanted.items()
        if (slug, version) not in accepted
    ]


def record_acceptance(user, slug: str, *, request=None) -> LegalAcceptance:
    """
    Write one acceptance for the version currently published.

    The version is taken from settings and never from the request: a client
    that could name the version it was accepting could claim to have accepted
    wording it was never shown.

    Idempotent through the model's unique constraint -- clicking accept twice
    returns the first row rather than making a second one.
    """
    version = current_version(slug)
    ip_address = None
    user_agent = ""
    if request is not None:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        ip_address = forwarded.split(",")[0].strip() or request.META.get("REMOTE_ADDR") or None
        user_agent = request.META.get("HTTP_USER_AGENT", "")[:400]

    acceptance, created = LegalAcceptance.objects.get_or_create(
        user=user,
        document=slug,
        version=version,
        defaults={
            "tenant_id": getattr(user, "tenant_id", None),
            "ip_address": ip_address,
            "user_agent": user_agent,
        },
    )
    if created:
        logger.info("User id=%s accepted %s v%s.", user.pk, slug, version)
    return acceptance


def history_for(user) -> list:
    """Every acceptance this user has ever made, newest first."""
    return list(LegalAcceptance.objects.filter(user=user))
