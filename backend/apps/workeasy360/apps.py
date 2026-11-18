from django.apps import AppConfig


class WorkEasy360Config(AppConfig):
    """Sign-in with WorkEasy360 Accounts. See docs/workeasy360-sso.md."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.workeasy360"
    label = "workeasy360"
    verbose_name = "WorkEasy360 Accounts"
