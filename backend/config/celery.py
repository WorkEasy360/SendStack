import os
import uuid

from celery import Celery
from celery.signals import before_task_publish, task_postrun, task_prerun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

app = Celery("sendstack")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


# --- Correlation ids across the queue ------------------------------------
#
# core.middleware.RequestIDMiddleware puts an id on every HTTP request and
# every log line it produces, which is worth very little for this application:
# almost nothing customer-visible finishes inside the request. "Send this
# campaign" returns immediately and the work happens in a worker minutes
# later, so the request id stops exactly where the interesting part starts.
#
# These three hooks carry it across. The id is stamped into the task's headers
# when it is published (in the web process, where the id exists), and restored
# into the worker's context while the task runs -- so a support question that
# starts with one id from the browser can be followed all the way to the
# provider call, through however many queue hops it took.
#
# A task published by another task inherits the same id, because the publisher
# is itself running inside a restored context. A task with no id at all -- one
# from celery beat, which answers to no request -- gets a fresh one rather than
# "-", so periodic work is still traceable as a single run.

REQUEST_ID_HEADER = "request_id"


@before_task_publish.connect
def _stamp_request_id(headers=None, **kwargs):
    from core.request_context import get_request_id

    if headers is None:
        return
    request_id = get_request_id()
    if request_id in (None, "", "-"):
        request_id = uuid.uuid4().hex
    headers.setdefault(REQUEST_ID_HEADER, request_id)


@task_prerun.connect
def _restore_request_id(task_id=None, task=None, **kwargs):
    from core.request_context import set_request_id

    request = getattr(task, "request", None)
    request_id = getattr(request, REQUEST_ID_HEADER, None) or task_id or uuid.uuid4().hex
    # Stashed on the task so the matching postrun can reset exactly this token
    # rather than guessing -- workers run tasks on pooled threads, and a
    # ContextVar reset with the wrong token raises.
    task._request_id_token = set_request_id(request_id)


@task_postrun.connect
def _clear_request_id(task=None, **kwargs):
    from core.request_context import reset_request_id

    token = getattr(task, "_request_id_token", None)
    if token is not None:
        try:
            reset_request_id(token)
        except ValueError:
            # Set in a different context than the one being torn down. The
            # var is per-context anyway, so there is nothing left to leak.
            pass
        task._request_id_token = None
