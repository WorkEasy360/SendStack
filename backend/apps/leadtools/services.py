"""
The work behind the lead-tool endpoints, kept out of the views so the task
and the tests can call it directly.

Two rules run through everything here:

* **Every contact these tools create goes through ``ContactImport``.** The
  importer already owns the plan's contact ceiling, the never-overwrite-a-
  hand-corrected-name rule and the consent model. Writing contacts any
  other way would be a second implementation of three things a customer
  would notice being applied inconsistently.

* **Consent is never asserted.** A ContactImport with a blank
  ``consent_source`` leaves every contact at UNKNOWN, which is the truth: a
  registration lookup and a group membership are both facts about a number,
  not permission from its owner.
"""

import csv
import io
import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from apps.contacts.models import Contact, ContactGroup, ContactImport
from apps.contacts.paste import parse as parse_paste
from apps.contacts.tasks import process_contact_import
from apps.whatsapp.connections.manager import ConnectionManager
from apps.whatsapp.models import WhatsAppAccount
from core.csv_utils import csv_safe_row

from .models import GroupGrab, NumberCheck, NumberCheckEntry

logger = logging.getLogger("sendstack")

#: The custom field a grabbed contact carries naming the WhatsApp group it
#: was read from. A column header in the CSV handed to the importer, so it
#: must not look like a phone, name, email, company or country column to
#: apps.contacts.mapping -- "WhatsApp group" is kept as a custom field.
GROUP_FIELD = "WhatsApp group"


class LeadToolError(ValueError):
    """A refusal written for the person who asked. Views answer it with 400."""


# --------------------------------------------------------------------------
# Accounts


def usable_account(tenant, account_id, capability: str) -> WhatsAppAccount:
    """
    The tenant's own, connected, QR-linked number -- or a LeadToolError that
    says which of those it is not.

    The capability check reads the provider's own flag rather than the
    connection method, so a deployment whose bridge is not configured says so
    instead of failing later inside a task.
    """
    account = WhatsAppAccount.objects.filter(tenant=tenant, pk=account_id).first()
    if account is None:
        raise LeadToolError("Pick one of your own WhatsApp numbers.")
    if account.connection_method != WhatsAppAccount.ConnectionMethod.QR:
        raise LeadToolError(
            "This tool works through a QR-linked number. Cloud API numbers cannot look up contacts or groups."
        )
    if account.status != WhatsAppAccount.Status.CONNECTED:
        raise LeadToolError(f"{account.name} is not connected. Reconnect it from Devices first.")
    if not ConnectionManager.supports(account, capability):
        raise LeadToolError("This deployment's WhatsApp connection does not support this tool.")
    return account


# --------------------------------------------------------------------------
# Number filter


def resolve_numbers(*, tenant, source, text="", default_country_code="", contact_group=None):
    """
    The distinct numbers a check will cover, as ``[(phone, contact_id), ...]``.

    A pasted list is read by the same parser the paste importer uses, so the
    two agree about what counts as a number. Rows that did not parse are
    simply not checked -- the screen has already shown them as unusable, and
    asking WhatsApp about "not-a-number" would only add an error row.
    """
    if source == NumberCheck.Source.PASTE:
        rows = parse_paste(text or "", default_country_code=default_country_code or "")
        phones = []
        seen = set()
        for row in rows:
            phone = row.get("phone")
            if phone and phone not in seen:
                seen.add(phone)
                phones.append(phone)
        if not phones:
            raise LeadToolError("No usable phone numbers were found in that list.")
        by_phone = dict(
            Contact.objects.filter(tenant=tenant, phone__in=phones).values_list("phone", "pk"),
        )
        return [(phone, by_phone.get(phone)) for phone in phones]

    queryset = Contact.objects.filter(tenant=tenant)
    if contact_group is not None:
        queryset = queryset.filter(groups=contact_group)
    pairs = list(queryset.order_by("pk").values_list("phone", "pk").distinct())
    if not pairs:
        raise LeadToolError("There are no contacts to check." if contact_group is None else "That group is empty.")
    return pairs


@transaction.atomic
def start_number_check(*, user, account, source, text="", default_country_code="", contact_group=None) -> NumberCheck:
    """Create the check and its rows, then hand the asking to the worker."""
    from .tasks import run_number_check

    tenant = user.tenant
    pairs = resolve_numbers(
        tenant=tenant, source=source, text=text,
        default_country_code=default_country_code, contact_group=contact_group,
    )
    ceiling = settings.LEADTOOLS_NUMBER_CHECK_MAX_NUMBERS
    if len(pairs) > ceiling:
        raise LeadToolError(
            f"That is {len(pairs):,} numbers; one check can cover at most {ceiling:,}. "
            "Check a group at a time, or split the list."
        )

    check = NumberCheck.objects.create(
        tenant=tenant, created_by=user, whatsapp_account=account,
        source=source, contact_group=contact_group, total=len(pairs),
    )
    NumberCheckEntry.objects.bulk_create([
        NumberCheckEntry(number_check=check, phone=phone, contact_id=contact_id) for phone, contact_id in pairs
    ], batch_size=1000)

    # After commit, so a worker that picks this up instantly can see the rows.
    transaction.on_commit(lambda: run_number_check.delay(check.pk))
    return check


def _import_rows(*, tenant, user, rows, headers, group, filename) -> ContactImport:
    """
    Feed ``rows`` to the ordinary contact importer as a CSV.

    A CSV rather than a direct write, on purpose: the importer is the one
    place the contact ceiling, the gap-filling rule and consent are enforced,
    and it reports what it did in a shape the screen already knows how to
    show. The file it leaves behind is also the record of exactly what was
    handed over.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(csv_safe_row(row))

    contact_import = ContactImport.objects.create(
        tenant=tenant, created_by=user,
        file=ContentFile(buffer.getvalue().encode("utf-8"), name=filename),
        group_ids=[group.pk],
    )
    transaction.on_commit(lambda: process_contact_import.delay(contact_import.pk))
    return contact_import


@transaction.atomic
def save_check_entries(check: NumberCheck, *, user, result: str, group_name: str):
    """
    Put every number with ``result`` into a contact group, creating contacts
    for the ones that are new. Returns ``(contact_import, contact_group)``.
    """
    entries = list(
        check.entries.filter(result=result).select_related("contact").order_by("pk"),
    )
    if not entries:
        raise LeadToolError("There are no numbers with that result to save.")

    group, _ = ContactGroup.objects.get_or_create(tenant=check.tenant, name=group_name[:100])
    rows = [(entry.contact.name if entry.contact else "", entry.phone) for entry in entries]
    contact_import = _import_rows(
        tenant=check.tenant, user=user, rows=rows, headers=["name", "phone"], group=group,
        filename=f"number-check-{check.pk}.csv",
    )
    return contact_import, group


# --------------------------------------------------------------------------
# Group grabber


def _group_dict(summary) -> dict:
    return {
        "id": summary.id,
        "subject": summary.subject,
        "member_count": summary.member_count,
        "announce": summary.announce,
        "is_community": summary.is_community,
        "created_at": summary.created_at.isoformat() if summary.created_at else None,
    }


def list_groups(account) -> list[dict]:
    return [_group_dict(summary) for summary in ConnectionManager.list_groups(account)]


@transaction.atomic
def grab_groups(*, user, account, group_ids, contact_group_name="") -> GroupGrab:
    """
    Read the members of ``group_ids`` and hand them to the importer.

    One import and one contact group for the whole grab, however many
    WhatsApp groups were ticked: somebody selecting five groups wants one
    list, and a member of three of them wants to be imported once. Which
    WhatsApp group each number came from is kept as a custom field on the
    contact, so the information is not lost -- it is just not a reason to
    make three contacts.
    """
    tenant = user.tenant
    fetched = ConnectionManager.get_group_members(account, list(group_ids))
    if not fetched:
        raise LeadToolError("None of those groups could be read.")

    rows = []
    seen = set()
    hidden = 0
    for group in fetched:
        hidden += group.hidden_count
        for member in group.members:
            if member.phone in seen:
                continue
            seen.add(member.phone)
            rows.append((member.name, member.phone, group.subject))
    if not rows:
        raise LeadToolError(
            "No members with a visible phone number were found. WhatsApp hides the numbers of members "
            "who are not in this phone's contacts in some groups."
        )

    name = (contact_group_name or "").strip()
    if not name:
        name = fetched[0].subject if len(fetched) == 1 and fetched[0].subject else "WhatsApp groups"
    group, _ = ContactGroup.objects.get_or_create(tenant=tenant, name=name[:100])

    contact_import = _import_rows(
        tenant=tenant, user=user, rows=rows, headers=["name", "phone", GROUP_FIELD], group=group,
        filename="whatsapp-groups.csv",
    )
    return GroupGrab.objects.create(
        tenant=tenant, created_by=user, whatsapp_account=account,
        groups=[
            {
                "id": group_members.id,
                "subject": group_members.subject,
                "member_count": len(group_members.members) + group_members.hidden_count,
                "hidden_count": group_members.hidden_count,
            }
            for group_members in fetched
        ],
        contact_group=group, contact_import=contact_import,
        members_found=len(rows), members_hidden=hidden,
    )
