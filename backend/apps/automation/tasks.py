import logging

from config.celery import app

from . import services

logger = logging.getLogger("multisender")


@app.task(bind=True, max_retries=3)
def check_and_send_follow_up(self, message_id, rule_id):
    try:
        services.run_follow_up_check(message_id, rule_id)
    except Exception as exc:
        # Retried rather than dropped: the alternative is a follow-up silently
        # never sent because the database blinked at the wrong moment.
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@app.task
def resume_workflow_run(run_id):
    from .models import WorkflowRun

    try:
        run = WorkflowRun.objects.select_related("workflow", "contact").get(pk=run_id)
    except WorkflowRun.DoesNotExist:
        return
    # No retry here on purpose. resume_workflow claims the run with a
    # conditional UPDATE and _advance records its own failures, so a retry
    # would either find the run already claimed (and do nothing) or replay
    # steps that have already sent messages.
    services.resume_workflow(run)


@app.task
def sweep_stuck_workflow_runs():
    """Periodic. See services.sweep_stuck_runs for why this is needed."""
    return services.sweep_stuck_runs()
