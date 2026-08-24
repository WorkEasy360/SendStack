from django.apps import AppConfig


class TeamsConfig(AppConfig):
    name = "apps.teams"
    verbose_name = "Team and agents"

    def ready(self):
        from . import receivers  # noqa: F401
