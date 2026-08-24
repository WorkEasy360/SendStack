import django.dispatch

# Sent once, immediately after an outbound message is accepted by the
# provider. `message` is the messaging.Message, already marked SENT.
#
# Exists so downstream apps (CRM stamps, lead scoring) can react to a real
# send without post_save on Message, which fires again for every delivery
# receipt and read receipt that follows.
outbound_message_sent = django.dispatch.Signal()
