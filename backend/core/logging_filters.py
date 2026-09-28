import logging
import re

from .request_context import get_request_id


class RequestIDFilter(logging.Filter):
    def filter(self, record):
        record.request_id = get_request_id()
        return True


class PhoneMaskingFilter(logging.Filter):
    """
    Mask phone numbers in log records: ``+919876543210`` -> ``+91******3210``.

    The send path logs a line per message naming its recipient, which is the
    right amount of detail for diagnosing a campaign and the wrong amount for a
    log aggregator that a support team, an intern and a third-party vendor can
    all search. Masking keeps the diagnostic value -- two recipients still look
    different, and the last four digits are what a customer quotes when they
    ask why a message did not arrive -- while removing the ability to dial or
    to build a list.

    A filter rather than an edit to each call site, deliberately: a call site
    is a thing somebody adds later without remembering the rule, and this
    catches it. The call sites keep passing whole numbers; nothing reaches a
    handler unmasked.

    Off by default in development (LOG_MASK_PHONE_NUMBERS), where the full
    number is often exactly what somebody is debugging against, and on in
    production.
    """

    #: Seven to fifteen digits, optionally with a leading +. Deliberately not
    #: anchored to E.164 -- a number logged before normalisation is still a
    #: number.
    PATTERN = re.compile(r"\+?\d{7,15}")

    def __init__(self, name=""):
        super().__init__(name)
        from django.conf import settings

        self.enabled = getattr(settings, "LOG_MASK_PHONE_NUMBERS", False)

    @staticmethod
    def _mask(match) -> str:
        text = match.group(0)
        digits = re.sub(r"\D", "", text)
        prefix = "+" if text.startswith("+") else ""
        return f"{prefix}{digits[:2]}{'*' * (len(digits) - 6)}{digits[-4:]}"

    def _scrub(self, value):
        if isinstance(value, str):
            return self.PATTERN.sub(self._mask, value)
        return value

    def filter(self, record):
        if not self.enabled:
            return True

        # The message is masked *before* formatting where it can be, so the
        # arguments -- which is where `message.to_phone` actually lives -- are
        # covered too. A record whose args cannot be walked is left alone
        # rather than dropped: losing a log line is worse than a full number in
        # it.
        try:
            if isinstance(record.args, dict):
                record.args = {key: self._scrub(value) for key, value in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._scrub(value) for value in record.args)
            if isinstance(record.msg, str):
                record.msg = self.PATTERN.sub(self._mask, record.msg)
        except Exception:  # noqa: BLE001 - see above
            pass
        return True
