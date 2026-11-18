"""
Mounted at ``/api/v1/auth/workeasy360/`` (config/urls.py), behind the same
nginx ``/api/`` location as the rest of the API.

The registered redirect URI is ``<origin>/api/v1/auth/workeasy360/callback/``
and the back-channel logout URI ``<origin>/api/v1/auth/workeasy360/backchannel-logout/``,
exactly, trailing slash included.
"""

from django.urls import path

from workeasy360_sso import views as sso_views
from workeasy360_sso.urls import namespaced

from . import views

urlpatterns = [
    namespaced(
        path("start/", sso_views.start, name="start"),
        path("callback/", sso_views.callback, name="callback"),
        path("logout/", sso_views.logout, name="logout"),
        path("logout/workeasy360/", sso_views.logout_workeasy360, name="logout_workeasy360"),
        path("backchannel-logout/", sso_views.backchannel_logout, name="backchannel_logout"),
        path("status/", sso_views.status, name="status"),
    ),
    path("session/", views.session_status, name="workeasy360-session"),
]
