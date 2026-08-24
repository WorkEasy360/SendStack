from django.urls import path

from .views import CustomerListView, EffectiveBrandingView, PublicBrandingView, TenantBrandingView

urlpatterns = [
    path("customers/", CustomerListView.as_view(), name="tenant-customers"),
    path("branding/", TenantBrandingView.as_view(), name="tenant-branding"),
    path("branding/effective/", EffectiveBrandingView.as_view(), name="tenant-branding-effective"),
    path("branding/public/", PublicBrandingView.as_view(), name="tenant-branding-public"),
]
