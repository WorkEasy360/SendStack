import django.dispatch

# Sent when a campaign moves to a status an external integration cares about:
# QUEUED/RUNNING on launch, CANCELLED, and COMPLETED when the last recipient
# reaches a terminal state.
#
# Needed because campaign status is not consistently written with `save()`.
# Completion in particular is a queryset `.update()` in
# apps.messaging.services.refresh_campaign_counters -- chosen there so two
# workers finishing the last two messages cannot both flip the row -- and a
# queryset update fires no post_save. So there is no model signal that sees a
# campaign complete, and without this one a webhook subscriber would learn
# about a finished campaign only by polling.
#
# `status` is passed explicitly for the same reason messaging's equivalent
# does: a receiver should react to the transition that happened, not to
# whatever the row says by the time it runs.
campaign_status_changed = django.dispatch.Signal()
