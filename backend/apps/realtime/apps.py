from django.apps import AppConfig


class RealtimeConfig(AppConfig):
    name = "apps.realtime"
    verbose_name = "Real-time updates"

    def ready(self):
        from . import receivers  # noqa: F401
