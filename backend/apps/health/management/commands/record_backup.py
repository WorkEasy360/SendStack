"""
Let the backup script tell the application what it did.

    python manage.py record_backup --status SUCCEEDED --artifact db-2026...dump.gpg \
        --bytes 41231872 --destination s3 --offsite --encrypted

Called by scripts/backup_postgres.sh at the end of every run, success or
failure, through `docker compose exec`. It is the only bridge between a host
cron job and the deployment's own view of whether it is protected.

**Never fails the caller.** A backup that succeeded and could not be recorded
is still a backup; exiting non-zero here would turn a monitoring gap into a
failed backup job and, worse, teach an operator to ignore the job's exit code.
Anything that goes wrong is printed and the command exits 0.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.health.models import BackupRun


class Command(BaseCommand):
    help = "Record the outcome of a backup or restore verification."

    def add_arguments(self, parser):
        parser.add_argument(
            "--status", required=True, choices=[value for value, _ in BackupRun.Status.choices],
        )
        parser.add_argument(
            "--kind", default=BackupRun.Kind.BACKUP,
            choices=[value for value, _ in BackupRun.Kind.choices],
        )
        parser.add_argument("--artifact", default="", help="The archive's basename. Not its path.")
        parser.add_argument("--bytes", type=int, default=0)
        parser.add_argument("--destination", default="", help="A label: s3, rclone, local.")
        parser.add_argument("--offsite", action="store_true", help="The archive left the host.")
        parser.add_argument("--encrypted", action="store_true")
        parser.add_argument("--detail", default="", help="Error text, or a short note.")

    def handle(self, *args, **options):
        try:
            run = BackupRun.objects.create(
                kind=options["kind"],
                status=options["status"],
                started_at=timezone.now(),
                finished_at=timezone.now(),
                # Basename only, defensively: the script is asked for one, and
                # a path that slipped through would end up in an API response.
                artifact=str(options["artifact"]).replace("\\", "/").rsplit("/", 1)[-1][:255],
                size_bytes=max(int(options["bytes"] or 0), 0),
                destination=str(options["destination"])[:64],
                offsite=bool(options["offsite"]),
                encrypted=bool(options["encrypted"]),
                detail=str(options["detail"])[:4000],
            )
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            self.stderr.write(
                self.style.WARNING(f"Could not record the backup run: {type(exc).__name__}.")
            )
            return

        self.stdout.write(self.style.SUCCESS(f"Recorded {run.kind} {run.status} (id={run.pk})."))
