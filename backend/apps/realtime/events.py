"""
The fan-out behind the live inbox.

Redis pub/sub rather than WebSockets and Channels: the inbox needs the server
to push, not a duplex channel, and Server-Sent Events give exactly that over
ordinary HTTP -- no ASGI server, no second protocol through nginx, and a
browser that reconnects on its own when the connection drops.

Everything here is best-effort. A live update is a convenience on top of an
API that already works; a Redis outage must slow nobody down and lose no
data, so publishing swallows its errors and the inbox falls back to polling.
"""

import json
import logging

import redis
from django.conf import settings

logger = logging.getLogger("multisender")

# Kept short so a Redis that has gone away cannot hold up the request that is
# publishing -- an inbound webhook must not wait on the live update.
_SOCKET_TIMEOUT = 0.25

_client = None
_client_failed = False


def _redis():
    """
    The connection used for publishing, made once and reused.

    A failure is remembered rather than retried on every message: with Redis
    down, retrying the connection per published event turns a degraded live
    update into a slow API.
    """
    global _client, _client_failed

    if _client_failed:
        return None
    if _client is None:
        try:
            _client = redis.Redis.from_url(
                settings.REDIS_URL,
                socket_timeout=_SOCKET_TIMEOUT,
                socket_connect_timeout=_SOCKET_TIMEOUT,
                health_check_interval=30,
            )
        except Exception:
            logger.warning("Real-time updates are unavailable: could not build a Redis client.", exc_info=True)
            _client_failed = True
            return None
    return _client


def reset_client():
    """Forget the cached connection. Used by tests, and after a configuration change."""
    global _client, _client_failed

    _client = None
    _client_failed = False


def channel(tenant_id) -> str:
    """
    One channel per tenant.

    Per-tenant rather than per-user so that a message arriving once is
    published once, and per-tenant rather than global so no subscriber ever
    receives another customer's traffic to filter out client-side.
    """
    return f"multisender:rt:{tenant_id}"


def publish(tenant_id, event_type: str, data: dict) -> bool:
    """Announce something to everyone watching this tenant. Never raises."""
    client = _redis()
    if client is None or not tenant_id:
        return False

    try:
        client.publish(channel(tenant_id), json.dumps({"type": event_type, "data": data}, default=str))
        return True
    except Exception:
        logger.debug("Could not publish %s for tenant %s.", event_type, tenant_id, exc_info=True)
        return False


def subscribe(tenant_id):
    """A pubsub handle already listening to this tenant, or None if Redis is unreachable."""
    client = _redis()
    if client is None:
        return None
    try:
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(channel(tenant_id))
        return pubsub
    except Exception:
        logger.warning("Could not subscribe to real-time updates for tenant %s.", tenant_id, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------

_PRESENCE_TTL_SECONDS = 90


def _presence_key(tenant_id, user_id) -> str:
    return f"multisender:presence:{tenant_id}:{user_id}"


def touch_presence(tenant_id, user_id) -> None:
    """
    Mark this user as here, for a while.

    A TTL rather than an explicit "went offline": a browser that is closed,
    crashes, or loses its network never gets to say goodbye, and a presence
    list full of people who left hours ago is worse than none.
    """
    client = _redis()
    if client is None:
        return
    try:
        client.setex(_presence_key(tenant_id, user_id), _PRESENCE_TTL_SECONDS, "1")
    except Exception:
        logger.debug("Could not record presence for user %s.", user_id, exc_info=True)


def online_user_ids(tenant_id) -> list:
    client = _redis()
    if client is None:
        return []
    try:
        prefix = _presence_key(tenant_id, "")
        return sorted(
            int(key.decode().rsplit(":", 1)[1])
            for key in client.scan_iter(match=f"{prefix}*", count=200)
        )
    except Exception:
        logger.debug("Could not read presence for tenant %s.", tenant_id, exc_info=True)
        return []
