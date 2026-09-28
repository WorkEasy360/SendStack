import django.dispatch

# Sent once, immediately after an outbound message is accepted by the
# provider. `message` is the messaging.Message, already marked SENT.
#
# Exists so downstream apps (CRM stamps, lead scoring) can react to a real
# send without post_save on Message, which fires again for every delivery
# receipt and read receipt that follows.
outbound_message_sent = django.dispatch.Signal()


# Sent whenever an outbound message reaches a new terminal-ish status that an
# external integration cares about: SENT/DELIVERED/READ from a provider
# receipt, and FAILED or SKIPPED from either the send task or the gate.
#
# Distinct from `outbound_message_sent` above, which fires exactly once at the
# moment of acceptance and is what the CRM stamps react to. This one fires on
# every forward transition, which is what a webhook subscriber needs and what a
# "last contacted" stamp very much does not.
#
# `status` is passed explicitly rather than read off the message, so a receiver
# reacts to the transition that happened rather than to whatever the row says
# by the time it runs.
message_status_changed = django.dispatch.Signal()
