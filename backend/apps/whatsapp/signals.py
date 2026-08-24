import django.dispatch

# Sent once per newly-stored (never redelivered/duplicate) webhook event, with
# the WhatsAppWebhookEvent instance as `event`. Lets other apps (messaging,
# and later inbox) react to inbound provider activity without this app
# needing to know anything about them.
webhook_event_stored = django.dispatch.Signal()
