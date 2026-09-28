"""
One shared ``POST <resource>/bulk/`` endpoint shape for every list the UI lets
you multi-select in.

Handlers run **per object**, inside the viewset's own tenant-scoped
``get_queryset()``, so a bulk call can never reach further than the equivalent
single-object call and every existing permission/validation rule still applies.
Running per object (rather than one ``queryset.delete()``) is deliberate: it is
what makes partial failure reportable -- deleting six campaigns where one is
still RUNNING deletes the other five and tells you precisely which one was
refused, instead of failing the whole batch or silently skipping it.
"""

import logging

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError as DjangoValidationError
from django.db.models import ProtectedError
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response

logger = logging.getLogger("sendstack")

# A safety rail, not a UI limit: the frontend selects at most a page at a time.
BULK_MAX_IDS = 500

# Past tense for the summary line. Naive "+ed" mangles half of these
# ("disconnectd", "reopend"), so the irregular ones are spelled out.
PAST_TENSE = {
    "add_group": "added to the group",
    "add_tag": "tagged",
    "cancel": "cancelled",
    "disconnect": "disconnected",
    "mark_read": "marked read",
    "remove_group": "removed from the group",
    "remove_tag": "untagged",
    "reopen": "reopened",
    "suspend": "suspended",
}


def past_tense(name):
    if name in PAST_TENSE:
        return PAST_TENSE[name]
    return f"{name}d" if name.endswith("e") else f"{name}ed"


class BulkActionError(Exception):
    """Raised by a handler to fail exactly one object with a readable reason."""


def _reason(exc):
    if isinstance(exc, ProtectedError):
        models = sorted({obj._meta.verbose_name for obj in exc.protected_objects})
        return f"Still referenced by {', '.join(models)}."
    if isinstance(exc, (ValidationError, DjangoValidationError)):
        detail = getattr(exc, "detail", None) or getattr(exc, "messages", None) or str(exc)
        if isinstance(detail, dict):
            detail = next(iter(detail.values()), detail)
        if isinstance(detail, (list, tuple)):
            detail = " ".join(str(part) for part in detail)
        return str(detail)
    if isinstance(exc, APIException):
        return str(exc.detail)
    return str(exc) or exc.__class__.__name__


class BulkActionMixin:
    """
    Mix into a ViewSet and declare ``bulk_actions``:

        bulk_actions = {"delete": "_bulk_delete", "activate": "_bulk_activate"}

    Each named method takes ``(request, instance)`` and either succeeds or
    raises. The endpoint reports how many of each there were.
    """

    bulk_actions: dict[str, str] = {}
    bulk_object_label = "item"

    def _bulk_label(self, count):
        noun = self.bulk_object_label
        return f"{count} {noun}" if count == 1 else f"{count} {noun}s"

    @action(detail=False, methods=["post"], url_path="bulk")
    def bulk(self, request):
        name = request.data.get("action")
        handler_name = self.bulk_actions.get(name)
        if handler_name is None:
            return Response(
                {"detail": f"Unsupported action. Choose one of: {', '.join(sorted(self.bulk_actions)) or '(none)'}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_ids = request.data.get("ids")
        if not isinstance(raw_ids, (list, tuple)):
            return Response({"detail": "Provide `ids` as a list."}, status=status.HTTP_400_BAD_REQUEST)
        # Preserve the caller's order but drop duplicates, so the per-id
        # results line up with what the user actually ticked.
        ids, seen = [], set()
        for value in raw_ids:
            if value not in seen:
                seen.add(value)
                ids.append(value)
        if not ids:
            return Response({"detail": "Select at least one item."}, status=status.HTTP_400_BAD_REQUEST)
        if len(ids) > BULK_MAX_IDS:
            return Response(
                {"detail": f"At most {BULK_MAX_IDS} items can be handled in one request ({len(ids)} given)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        handler = getattr(self, handler_name)
        queryset = self.get_queryset()
        found = {str(obj.pk): obj for obj in queryset.filter(pk__in=ids)}

        succeeded, errors = [], []
        for raw_id in ids:
            instance = found.get(str(raw_id))
            if instance is None:
                # Out of scope for this user, or already gone -- either way it
                # is reported rather than silently dropped from the tally.
                errors.append({"id": raw_id, "detail": "Not found."})
                continue
            try:
                handler(request, instance)
            except (BulkActionError, ValidationError, DjangoValidationError, ProtectedError,
                    APIException, PermissionDenied, ObjectDoesNotExist) as exc:
                errors.append({"id": raw_id, "detail": _reason(exc)})
            else:
                succeeded.append(instance.pk)

        payload = {
            "action": name,
            "requested": len(ids),
            "succeeded": len(succeeded),
            "failed": len(errors),
            "succeeded_ids": succeeded,
            "errors": errors,
            "detail": self._bulk_detail(name, len(succeeded), len(errors), len(ids)),
        }
        logger.info(
            "Bulk %s on %s: %s/%s succeeded", name, self.bulk_object_label, len(succeeded), len(ids),
        )
        if not succeeded:
            return Response(payload, status=status.HTTP_400_BAD_REQUEST)
        if errors:
            return Response(payload, status=status.HTTP_207_MULTI_STATUS)
        return Response(payload)

    def _bulk_detail(self, name, succeeded, failed, requested):
        verb = past_tense(name)
        if not succeeded:
            return f"No {self.bulk_object_label} could be {verb}."
        if failed:
            return f"{self._bulk_label(succeeded)} of {requested} {verb}; {failed} could not be."
        return f"{self._bulk_label(succeeded)} {verb}."
