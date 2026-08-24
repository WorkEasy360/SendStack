from django.apps import AppConfig


class InboxConfig(AppConfig):
    name = "apps.inbox"

    def ready(self):
        from . import receivers  # noqa: F401
