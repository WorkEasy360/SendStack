from django.urls import path

from .views import (
    AccountDeletionRequestView,
    BusinessProfileView,
    CustomerListView,
    EffectiveBrandingView,
    PublicBrandingView,
    TenantBrandingView,
    TenantExportView,
    TenantSettingsView,
    TenantStorageView,
)

urlpatterns = [
    path("customers/", CustomerListView.as_view(), name="tenant-customers"),
    path("business-profile/", BusinessProfileView.as_view(), name="tenant-business-profile"),
    path("branding/", TenantBrandingView.as_view(), name="tenant-branding"),
    path("branding/effective/", EffectiveBrandingView.as_view(), name="tenant-branding-effective"),
    path("branding/public/", PublicBrandingView.as_view(), name="tenant-branding-public"),
    path("export/", TenantExportView.as_view(), name="tenant-export"),
    # This workspace's own sending pace and housekeeping horizon.
    path("settings/", TenantSettingsView.as_view(), name="tenant-settings"),
    # What it is storing, and the one thing it may clean up.
    path("storage/", TenantStorageView.as_view(), name="tenant-storage"),
    # Records a request and starts a cooling-off window. Deletes nothing --
    # that stays with `manage.py delete_tenant` and its guards.
    path("deletion-request/", AccountDeletionRequestView.as_view(), name="tenant-deletion-request"),
]
