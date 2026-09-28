from django.apps import AppConfig


class PublicAPIConfig(AppConfig):
    name = "apps.publicapi"
    verbose_name = "Developer API"

    def ready(self):
        from . import receivers  # noqa: F401
