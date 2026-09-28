from django.conf import settings
from django.db import models


class LegalAcceptance(models.Model):
    """
    One person accepting one version of one document, at one moment.

    Append-only, and that is the entire design. The obvious alternative -- a
    ``terms_accepted`` boolean on the user -- cannot answer the only question
    anybody ever actually asks, which is *which wording* somebody agreed to.
    When the Terms change from 1.0 to 1.1, a boolean silently starts claiming
    the user accepted 1.1, and the record becomes evidence of something that
    never happened.

    So: a row per acceptance, never updated, never deleted by the application.
    Rows accumulate as versions do, and "which version did this user accept and
    when" is a query rather than a guess.

    The IP address and user agent are recorded because an acceptance nobody can
    corroborate is weak evidence, and because both are already in the request.
    They are the only two extra facts collected -- see the field comments.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="legal_acceptances",
    )
    # Denormalised from the user rather than followed through it: the question
    # "has this workspace accepted the current terms" must stay answerable
    # after the person who clicked accept has left the company and their
    # account has been deleted.
    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="legal_acceptances", null=True, blank=True,
    )

    document = models.CharField(max_length=32, help_text="Document slug, e.g. 'terms'.")
    version = models.CharField(max_length=16)
    accepted_at = models.DateTimeField(auto_now_add=True)

    # Two facts and no more. An acceptance with nothing around it is a bare
    # assertion; these two are already in the request, are proportionate to
    # the purpose, and are what a dispute actually turns on. Nothing else about
    # the person or their device is collected here.
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)

    class Meta:
        ordering = ["-accepted_at"]
        indexes = [
            # "What is outstanding for this user" -- the check that runs on
            # every page load of the app shell.
            models.Index(fields=["user", "document", "-accepted_at"]),
            models.Index(fields=["tenant", "document", "-accepted_at"]),
        ]
        constraints = [
            # One row per (user, document, version). A user who clicks accept
            # twice has agreed to the same wording twice, which is not a second
            # fact -- and without this a page refresh would inflate the trail.
            models.UniqueConstraint(
                fields=["user", "document", "version"], name="unique_legal_acceptance_per_version",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} accepted {self.document} v{self.version}"
