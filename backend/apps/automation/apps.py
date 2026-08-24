from django.apps import AppConfig


class AutomationConfig(AppConfig):
    name = "apps.automation"

    def ready(self):
        from . import receivers  # noqa: F401
