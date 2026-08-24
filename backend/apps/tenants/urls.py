from django.urls import path

from .views import (
    BusinessProfileView,
    CustomerListView,
    EffectiveBrandingView,
    PublicBrandingView,
    TenantBrandingView,
)

urlpatterns = [
    path("customers/", CustomerListView.as_view(), name="tenant-customers"),
    path("business-profile/", BusinessProfileView.as_view(), name="tenant-business-profile"),
    path("branding/", TenantBrandingView.as_view(), name="tenant-branding"),
    path("branding/effective/", EffectiveBrandingView.as_view(), name="tenant-branding-effective"),
    path("branding/public/", PublicBrandingView.as_view(), name="tenant-branding-public"),
]
