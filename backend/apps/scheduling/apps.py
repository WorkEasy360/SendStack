from django.apps import AppConfig


class SchedulingConfig(AppConfig):
    name = "apps.scheduling"

    def ready(self):
        # Importing the handlers module is what populates the registry the
        # runner dispatches through. Without it a worker booted for a queue
        # that never imports apps.campaigns would find every due job
        # unroutable and fail it -- see apps.scheduling.registry.
        from . import handlers  # noqa: F401
