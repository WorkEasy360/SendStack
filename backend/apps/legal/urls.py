from django.urls import path

from .views import LegalAcceptView, LegalDocumentListView, LegalDocumentView, LegalPendingView

urlpatterns = [
    path("documents/", LegalDocumentListView.as_view(), name="legal-documents"),
    path("documents/<str:slug>/", LegalDocumentView.as_view(), name="legal-document"),
    path("pending/", LegalPendingView.as_view(), name="legal-pending"),
    path("accept/", LegalAcceptView.as_view(), name="legal-accept"),
]
