from django.apps import AppConfig


class MessagingConfig(AppConfig):
    name = "apps.messaging"

    def ready(self):
        from . import receivers  # noqa: F401
