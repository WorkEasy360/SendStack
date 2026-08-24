from django.urls import path

from .views import PresenceView, RealtimeStatusView, StreamTicketView, TypingView, stream

urlpatterns = [
    path("ticket/", StreamTicketView.as_view(), name="realtime-ticket"),
    path("stream/", stream, name="realtime-stream"),
    path("typing/", TypingView.as_view(), name="realtime-typing"),
    path("presence/", PresenceView.as_view(), name="realtime-presence"),
    path("status/", RealtimeStatusView.as_view(), name="realtime-status"),
]
