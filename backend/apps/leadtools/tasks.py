"""
The number filter's worker: ask WhatsApp about each number, in paced batches.

Pacing is the point of running this off the request. A linked device that
fires ten thousand contact queries back to back looks like exactly the
scraping WhatsApp bans numbers for, and a banned sender is a far worse
outcome than a check that takes four minutes. The batch size and the pause
between batches are settings (LEADTOOLS_NUMBER_CHECK_*), and the bridge
paces its own inner loop on top of this -- see checkNumbers() in
qr-bridge/src/providers/baileysProvider.ts.
"""

import logging
import time

from django.conf import settings
from django.utils import timezone

from apps.contacts.models import Contact
from apps.whatsapp.connections.manager import ConnectionManager
from apps.whatsapp.providers.base import ProviderError
from config.celery import app

from .models import NumberCheck, NumberCheckEntry

logger = logging.getLogger("sendstack")


def _finish(check: NumberCheck, status: str, error: str = "") -> None:
    check.status = status
    check.error = error[:500]
    check.completed_at = timezone.now()
    check.save(update_fields=["status", "error", "completed_at"])


@app.task(bind=True)
def run_number_check(self, check_id: int):
    try:
        check = NumberCheck.objects.select_related("tenant", "whatsapp_account").get(pk=check_id)
    except NumberCheck.DoesNotExist:
        logger.warning("NumberCheck %s no longer exists.", check_id)
        return

    # Only a PENDING check is started. A redelivered task for a RUNNING one
    # would double-ask every remaining number; a COMPLETED or FAILED one is
    # a record now, not work.
    if check.status != NumberCheck.Status.PENDING:
        logger.info("NumberCheck %s is %s; not starting it again.", check_id, check.status)
        return
    check.status = NumberCheck.Status.RUNNING
    check.started_at = timezone.now()
    check.save(update_fields=["status", "started_at"])

    account = check.whatsapp_account
    batch_size = max(1, int(settings.LEADTOOLS_NUMBER_CHECK_BATCH_SIZE))
    pause = max(0.0, float(settings.LEADTOOLS_NUMBER_CHECK_PAUSE_SECONDS))
    on_status = Contact.WhatsAppStatus.ON_WHATSAPP
    off_status = Contact.WhatsAppStatus.NOT_ON_WHATSAPP

    try:
        while True:
            entries = list(
                check.entries.filter(result=NumberCheckEntry.Result.PENDING).order_by("pk")[:batch_size],
            )
            if not entries:
                break

            lookups = ConnectionManager.check_numbers(account, [entry.phone for entry in entries])
            by_phone = {lookup.phone: lookup for lookup in lookups}

            now = timezone.now()
            on, off, errors = [], [], 0
            for entry in entries:
                lookup = by_phone.get(entry.phone)
                entry.checked_at = now
                if lookup is None:
                    entry.result = NumberCheckEntry.Result.ERROR
                    errors += 1
                elif lookup.exists:
                    entry.result = NumberCheckEntry.Result.ON_WHATSAPP
                    entry.jid = (lookup.jid or "")[:64]
                    on.append(entry.phone)
                else:
                    entry.result = NumberCheckEntry.Result.NOT_ON_WHATSAPP
                    off.append(entry.phone)
            NumberCheckEntry.objects.bulk_update(entries, ["result", "jid", "checked_at"], batch_size=1000)

            # Stamp the contacts as well, by phone rather than by the entry's
            # contact link: a number pasted today and added as a contact
            # tomorrow should not need checking twice.
            if on:
                Contact.objects.filter(tenant=check.tenant, phone__in=on).update(
                    whatsapp_status=on_status, whatsapp_checked_at=now,
                )
            if off:
                Contact.objects.filter(tenant=check.tenant, phone__in=off).update(
                    whatsapp_status=off_status, whatsapp_checked_at=now,
                )

            check.checked += len(entries)
            check.on_whatsapp_count += len(on)
            check.not_on_whatsapp_count += len(off)
            check.error_count += errors
            check.save(update_fields=["checked", "on_whatsapp_count", "not_on_whatsapp_count", "error_count"])

            if pause and check.entries.filter(result=NumberCheckEntry.Result.PENDING).exists():
                time.sleep(pause)
    except ProviderError as exc:
        # Everything answered so far stays answered. The rows still PENDING
        # are exactly the ones that were never asked, which is what the
        # screen should say rather than guessing at them.
        logger.warning("NumberCheck %s stopped: %s (code=%s)", check_id, exc.message, exc.code)
        _finish(check, NumberCheck.Status.FAILED, exc.message or "WhatsApp could not be asked.")
        return
    except Exception as exc:  # noqa: BLE001 - always finish the record, even on the unexpected
        logger.exception("NumberCheck %s failed", check_id)
        _finish(check, NumberCheck.Status.FAILED, str(exc) or "The check failed unexpectedly.")
        return

    _finish(check, NumberCheck.Status.COMPLETED)
