"""
Vouch that an existing SendStack account is a WorkEasy360 person.

    python manage.py w360_link_user <user id> <workeasy360 sub> --actor "<who>" [--issuer <issuer>]

People are linked on ``(issuer, sub)``, never by email, so an existing
password account and the same person's first WorkEasy360 sign-in are two
different things -- the sign-in is refused (``account_conflict``) when the
addresses are the same. This command is the deliberate way to join them: the
account keeps its workspace, its history and its credits, and from then on
it signs in through WorkEasy360 only:

* its password is retired and every SimpleJWT session it had is ended;
* its workspace must already be linked to the person's organization
  (``w360_link_organization``), so the next sign-in lands in it.

Refused for reseller and platform accounts, an account already linked under
that issuer, and a ``sub`` that already belongs to another account. Written
to the audit log.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.audit.models import AuditEvent
from apps.workeasy360 import services


class Command(BaseCommand):
    help = "Link an existing SendStack account to a WorkEasy360 person (issuer, sub)."

    def add_arguments(self, parser):
        parser.add_argument("user_id", type=int)
        parser.add_argument("subject", help="The person's WorkEasy360 sub (their central user id).")
        parser.add_argument("--actor", required=True, help="Who is vouching, for the audit log.")
        parser.add_argument("--issuer", default="", help="Defaults to WORKEASY360_ISSUER.")

    @transaction.atomic
    def handle(self, *args, **options):
        from workeasy360_sso.models import FederatedIdentity

        cfg = services.config()
        issuer = (options["issuer"] or (cfg.issuer if cfg is not None else "")).strip()
        if not issuer:
            raise CommandError("No issuer: set WORKEASY360_ISSUER or pass --issuer.")
        subject = options["subject"].strip()
        if not subject or len(subject) > 255:
            raise CommandError("The sub must be 1-255 characters.")

        user_model = get_user_model()
        user = user_model.objects.select_for_update().filter(pk=options["user_id"], is_deleted=False).first()
        if user is None:
            raise CommandError(f"No account with id {options['user_id']}.")
        if user.role != user_model.Role.USER or user.is_staff or user.is_superuser:
            raise CommandError("Only an ordinary user account can be linked; not a reseller or platform account.")
        link = services.link_for_tenant(user.tenant_id)
        if link is None:
            raise CommandError(
                f"The account's workspace ({user.tenant_id}) is not linked to a WorkEasy360 organization. "
                "Run w360_link_organization first."
            )
        if FederatedIdentity.objects.filter(issuer=issuer, subject=subject).exists():
            raise CommandError("That WorkEasy360 person already has an account here. Accounts are never merged.")
        if FederatedIdentity.objects.filter(user=user, issuer=issuer).exists():
            raise CommandError("That account is already linked to a WorkEasy360 person under this issuer.")

        FederatedIdentity.objects.create(user=user, issuer=issuer, subject=subject, email_at_link=user.email or "")
        user.set_unusable_password()
        user.save(update_fields=["password"])
        revoked = services.revoke_password_sessions(user)
        AuditEvent.objects.create(
            tenant_id=user.tenant_id, action="workeasy360.user_linked",
            object_type="accounts.User", object_id=str(user.pk),
            metadata={"issuer": issuer, "actor": options["actor"][:150], "password_sessions_ended": revoked},
        )
        self.stdout.write(self.style.SUCCESS(
            f"Account {user.pk} is WorkEasy360 person {subject} under {issuer}. Its password is retired "
            f"and {revoked} password session(s) ended."
        ))
