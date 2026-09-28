"""Background housekeeping for the operations app."""

import logging

from config.celery import app

logger = logging.getLogger("sendstack")


@app.task(name="apps.health.tasks.run_retention_sweep")
def run_retention_sweep(only: str = ""):
    """
    Delete the operational rows that have aged out. See apps.health.retention.

    Routed to the maintenance queue by CELERY_TASK_ROUTES, which is the one
    place queues are decided. This pass can spend minutes
    deleting a backlog, and a housekeeping job is never the thing that should
    be occupying the worker a customer's campaign is waiting behind.

    Deliberately not retried. A pass that fails leaves the rows exactly where
    they were and the next scheduled pass picks them up; retrying immediately
    would run the same long delete against the same struggling database.
    """
    from apps.health import retention

    results = retention.run_all(only=only or None)
    logger.info(
        "retention sweep finished",
        extra={"event": "retention.sweep", "result": results},
    )
    return results
