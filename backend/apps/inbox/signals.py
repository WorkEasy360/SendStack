import django.dispatch

# Sent after an inbound WhatsApp message has been turned into a Conversation +
# Message. `message` is the created messaging.Message; `is_new_conversation`
# tells subscribers (e.g. automation's welcome-message trigger) whether this
# was the contact's first-ever message on this account.
inbound_message_recorded = django.dispatch.Signal()
