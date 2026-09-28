"""
The nightly maintenance the CRM needs and nothing else can trigger.

Both jobs here exist because of *absence*: a lead score decays because time
passed, and a contact goes inactive because nothing happened. There is no
event to hang either on, so they run on a schedule.
"""

import logging

from config.celery import app

logger = logging.getLogger("sendstack")


@app.task
def refresh_crm_signals():
    """
    Rescore every tenant's contacts and cool down the ones who went quiet.

    Runs once a day rather than continuously: recency bands are measured in
    days, so a score computed at 3am is correct all day, and the alternative
    -- recomputing on a timer -- would be a full table pass every few minutes
    to change almost nothing.

    One tenant failing must not stop the rest, so each is wrapped separately.
    """
    from apps.tenants.models import Tenant

    from . import lifecycle, segments
    from .scoring import rescore_tenant

    scored = cooled = tenants = 0
    for tenant in Tenant.objects.filter(is_active=True).iterator(chunk_size=50):
        try:
            scored += rescore_tenant(tenant)
            cooled += lifecycle.mark_inactive_where_silent(tenant)
            segments.invalidate_counts(tenant.pk)
            tenants += 1
        except Exception:  # noqa: BLE001 - one bad tenant must not stall the sweep
            logger.exception("CRM signal refresh failed for tenant %s", tenant.pk)

    logger.info("CRM refresh: %s tenants, %s contacts rescored, %s marked inactive.", tenants, scored, cooled)
    return {"tenants": tenants, "rescored": scored, "cooled": cooled}
