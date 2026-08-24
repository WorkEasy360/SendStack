from django.conf import settings
from django.db import connections
from django.db.utils import OperationalError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

import redis


def _check_database():
    try:
        connections["default"].cursor()
        return True
    except OperationalError:
        return False


def _check_redis():
    try:
        client = redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        return bool(client.ping())
    except redis.RedisError:
        return False


class LivenessView(APIView):
    """The process is up and able to serve requests. Never checks dependencies."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"status": "alive"})


class ReadinessView(APIView):
    """The process and its critical dependencies (database, Redis) are usable."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        checks = {
            "database": _check_database(),
            "redis": _check_redis(),
        }
        healthy = all(checks.values())
        return Response(
            {"status": "ready" if healthy else "not_ready", "checks": checks},
            status=200 if healthy else 503,
        )


class HealthView(ReadinessView):
    """Alias of the readiness check, kept for /health/ per the deployment spec."""
