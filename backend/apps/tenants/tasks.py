"""Background housekeeping a workspace has asked for itself."""

import logging

from config.celery import app

logger = logging.getLogger("sendstack")


@app.task(name="apps.tenants.tasks.run_storage_cleanup")
def run_storage_cleanup():
    """
    Apply each workspace's own campaign-data horizon. See apps.tenants.storage.

    Deliberately a **separate** task from `apps.health.tasks.run_retention_sweep`
    rather than another rule inside it. That module's contract is that it never
    touches customer data, and it keeps that promise by having no rule that
    names any; a customer-data rule living there would make its docstring a
    lie for every reader afterwards. This one deletes customer data -- finished
    campaigns and nothing else -- and it does so only where the customer has
    set a horizon, which by default nobody has.

    Routed to the maintenance queue for the same reason the retention sweep is:
    a deletion pass must never be the thing a campaign is queued behind.

    Not retried. A failed pass leaves every row exactly where it was and the
    next scheduled pass picks it up; retrying immediately would run the same
    long delete against the same struggling database.
    """
    from .storage import prune_all

    results = prune_all()
    removed = sum(count for count in results.values() if count)
    logger.info(
        "storage cleanup finished",
        extra={"event": "storage.sweep", "workspaces": len(results), "campaigns": removed},
    )
    return results
