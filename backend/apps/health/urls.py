from django.urls import path

from .views import (
    BackupStatusView,
    DeadLetterView,
    DiagnosticsView,
    HealthView,
    LivenessView,
    OperationsView,
    ReadinessView,
)

urlpatterns = [
    path("", HealthView.as_view(), name="health"),
    path("ready/", ReadinessView.as_view(), name="health_ready"),
    path("live/", LivenessView.as_view(), name="health_live"),
    # Authenticated and tenant-scoped, unlike the two probes above. Mounted
    # here rather than under /api/v1/ because it is the same subject -- "is
    # this thing working" -- and support asks for it by name.
    path("diagnostics/", DiagnosticsView.as_view(), name="health_diagnostics"),
    # Superadmin only. Outcomes and ages, never a backup path or destination.
    path("backups/", BackupStatusView.as_view(), name="health_backups"),
    # The operations dashboard: queue depths, dead-lettered jobs, scheduler
    # backlog, provider states, backup freshness. Superadmin only, and 404 for
    # everybody else -- see the views for what it deliberately does not report.
    path("operations/", OperationsView.as_view(), name="health_operations"),
    path("dead-letters/", DeadLetterView.as_view(), name="health_dead_letters"),
]
