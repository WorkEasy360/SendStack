from django.apps import AppConfig


class HealthConfig(AppConfig):
    name = "apps.health"

    def ready(self):
        # Connects the Celery `task_failure` receiver that records a job's
        # final failure. Importing it here rather than from config.celery
        # keeps the app's own concerns inside the app, and means the receiver
        # exists in the web process too -- where `manage.py` runs tasks
        # eagerly in tests, and where the same signal fires.
        from . import deadletters  # noqa: F401
