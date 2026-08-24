from django.apps import AppConfig


class AIConfig(AppConfig):
    name = "apps.ai"
    label = "ai"
    verbose_name = "AI agent"

    def ready(self):
        from . import receivers  # noqa: F401
