import re

_E164_RE = re.compile(r"\+[1-9]\d{7,14}")


def normalize_phone(raw: str) -> str:
    """Normalize a phone number to E.164, raising ValueError if it can't be."""
    normalized = "+" + re.sub(r"\D", "", raw or "")
    if not _E164_RE.fullmatch(normalized):
        raise ValueError(f"'{raw}' is not a valid phone number.")
    return normalized
