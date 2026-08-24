from django.apps import AppConfig


class LeadgenConfig(AppConfig):
    name = "apps.leadgen"
    verbose_name = "Lead capture"

    def ready(self):
        from . import receivers  # noqa: F401
