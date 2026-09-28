"""
`manage.py check_production` -- the gate a release goes through.

Exits non-zero on any FAIL, so a deployment pipeline can simply run it and
stop. Warnings do not fail the command: a deployment with the AI provider
switched off is not broken, and a check that cried wolf would be turned off
within a week.
"""

from django.core.management.base import BaseCommand

from apps.health.checks import FAIL, PASS, WARN, run_all, summarise


class Command(BaseCommand):
    help = "Report whether this deployment is configured safely for production. Values are never printed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Exit non-zero on warnings as well as failures.",
        )

    def handle(self, *args, **options):
        results = run_all()
        styles = {
            PASS: self.style.SUCCESS,
            WARN: self.style.WARNING,
            FAIL: self.style.ERROR,
        }

        self.stdout.write("")
        for result in results:
            style = styles[result["status"]]
            label = result["status"].ljust(8)
            self.stdout.write(f"{style(label)} {result['name']}")
            if result["status"] != PASS:
                self.stdout.write(f"         {result['detail']}")

        counts = summarise(results)
        self.stdout.write("")
        self.stdout.write(
            f"{counts['passed']} passed, {counts['warnings']} warning(s), {counts['failed']} failed."
        )

        if counts["failed"]:
            self.stderr.write(
                self.style.ERROR("Not ready for production: fix the failures above.")
            )
            raise SystemExit(1)
        if options["strict"] and counts["warnings"]:
            self.stderr.write(self.style.WARNING("Warnings present and --strict was given."))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("No blocking failures."))
