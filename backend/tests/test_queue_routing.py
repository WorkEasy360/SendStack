"""
Queue routing is consistent between the settings, the worker and the dashboard.

Routing has one failure mode and it is silent: a task routed to a queue that no
worker consumes does not error, it simply waits in Redis for ever, which looks
exactly like an idle system. So the things that have to agree are asserted
against each other rather than each being assumed correct.

    settings.CELERY_TASK_ROUTES   names the queues tasks go to
    settings.CELERY_QUEUE_NAMES   the canonical list
    docker-compose.yml  -Q ...    what the shipped worker consumes
"""

import re
import unittest
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

REPO_ROOT = Path(settings.BASE_DIR).parent
COMPOSE = REPO_ROOT / "docker-compose.yml"

needs_repo = unittest.skipUnless(COMPOSE.exists(), "not running from a repository checkout")


class RouteConfigurationTests(SimpleTestCase):
    def test_every_routed_queue_is_a_declared_queue(self):
        """
        A typo in a route is otherwise undetectable.

        `{"queue": "mesages"}` creates that queue on first publish, no worker
        consumes it, and the tasks disappear.
        """
        declared = set(settings.CELERY_QUEUE_NAMES)

        for task, route in settings.CELERY_TASK_ROUTES.items():
            with self.subTest(task=task):
                self.assertIn(
                    route["queue"], declared,
                    f"{task} is routed to '{route['queue']}', which is not in CELERY_QUEUE_NAMES",
                )

    def test_the_default_queue_is_declared(self):
        self.assertIn(settings.CELERY_TASK_DEFAULT_QUEUE, settings.CELERY_QUEUE_NAMES)

    def test_every_routed_task_actually_exists(self):
        """
        A route naming a task that has been renamed routes nothing.

        It is not an error at any point -- the route simply never matches, the
        task keeps going to the default queue, and the separation an operator
        thinks they configured is not there.
        """
        from django.utils.module_loading import import_string

        for task in settings.CELERY_TASK_ROUTES:
            with self.subTest(task=task):
                module, _, name = task.rpartition(".")
                try:
                    import_string(f"{module}.{name}")
                except ImportError as exc:  # pragma: no cover - the failure message is the point
                    self.fail(f"CELERY_TASK_ROUTES names {task}, which does not exist: {exc}")

    def test_the_send_path_and_bulk_imports_are_on_different_queues(self):
        """
        The separation this phase exists to create.

        A 200,000-row import used to sit in front of every campaign send; the
        whole point of routing is that it no longer can, and that is only true
        if these two are not the same queue.
        """
        routes = settings.CELERY_TASK_ROUTES
        send = routes["apps.messaging.tasks.send_message_task"]["queue"]
        importing = routes["apps.contacts.tasks.process_contact_import"]["queue"]

        self.assertNotEqual(send, importing)

    def test_the_scheduler_tick_is_not_on_the_maintenance_queue(self):
        """
        The scheduler is what turns a scheduled campaign into work at all.

        Putting it behind an hour-long retention delete is how a customer's
        Friday campaign goes out on Saturday.
        """
        tick = settings.CELERY_TASK_ROUTES["apps.scheduling.tasks.run_due_scheduled_jobs"]["queue"]

        self.assertNotEqual(tick, "maintenance")

    def test_the_queue_list_is_short(self):
        """
        Deliberately few queues.

        Every queue is a thing an operator has to remember to consume, and a
        forgotten one is silent. Six workload shapes is where splitting stops
        paying for itself; the sixth is `webhooks`, which earns its place
        because it is the only queue whose latency is set by somebody else's
        server -- five attempts against a dead endpoint would otherwise sit in
        front of every automation and AI reply on the default queue.

        The ceiling being exactly the current count is on purpose: a seventh
        queue should be a deliberate edit to this line and an argument for why.
        """
        self.assertLessEqual(len(settings.CELERY_QUEUE_NAMES), 6)


@needs_repo
class ShippedWorkerTests(SimpleTestCase):
    """
    The worker in docker-compose.yml consumes everything the app routes to.

    This is the assertion that prevents the silent failure. Without it, adding
    a queue to settings and forgetting docker-compose is a change that passes
    every test and loses every task sent to the new queue.
    """

    def _worker_queues(self):
        text = COMPOSE.read_text(encoding="utf-8")
        # The -Q value, in either the list or the string command form.
        match = re.search(r"-Q\s*\n\s*-\s*([a-z,._-]+)", text) or re.search(
            r'"-Q",\s*"([a-z,._-]+)"', text
        )
        self.assertIsNotNone(match, "the celery worker in docker-compose.yml has no -Q")
        return {name.strip() for name in match.group(1).split(",") if name.strip()}

    def test_the_worker_consumes_every_declared_queue(self):
        consumed = self._worker_queues()

        missing = set(settings.CELERY_QUEUE_NAMES) - consumed
        self.assertEqual(
            missing, set(),
            f"docker-compose.yml's worker does not consume {sorted(missing)} -- "
            "tasks routed there would wait in Redis for ever",
        )

    def test_the_worker_consumes_nothing_undeclared(self):
        """A queue in -Q that the app never publishes to is dead configuration."""
        extra = self._worker_queues() - set(settings.CELERY_QUEUE_NAMES)

        self.assertEqual(extra, set(), f"docker-compose.yml consumes undeclared queues: {sorted(extra)}")


class BeatScheduleTests(SimpleTestCase):
    def test_the_retention_sweep_is_scheduled(self):
        """
        The retention rules only matter if something runs them.

        A pruning module nobody calls is the state this phase found the
        deployment in -- BILLING_WEBHOOK_PAYLOAD_RETENTION_DAYS was a setting
        read by nothing at all.
        """
        tasks = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}

        self.assertIn("apps.health.tasks.run_retention_sweep", tasks)

    def test_every_scheduled_task_exists(self):
        from django.utils.module_loading import import_string

        for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
            with self.subTest(entry=name):
                module, _, attribute = entry["task"].rpartition(".")
                try:
                    import_string(f"{module}.{attribute}")
                except ImportError as exc:  # pragma: no cover
                    self.fail(f"beat schedule '{name}' names {entry['task']}, which does not exist: {exc}")
