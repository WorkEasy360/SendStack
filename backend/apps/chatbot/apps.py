from django.apps import AppConfig


class ChatbotConfig(AppConfig):
    name = "apps.chatbot"
    verbose_name = "Chatbot"

    def ready(self):
        # Receivers only. The engine itself is never imported at startup --
        # it reaches into ai, automation, messaging and crm, and importing it
        # here would make this app's load order depend on all four.
        from . import receivers  # noqa: F401
