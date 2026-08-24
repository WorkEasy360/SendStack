"""
Working out what the columns in somebody's CSV actually are.

Nobody exports a file with the headers this platform would have chosen. They
export "Mobile No.", "Customer Name", "Firm" -- and then a column-mapping
screen asks them to explain, one dropdown at a time, what those obviously
mean. The mapping screen is the single most common place a contact import is
abandoned, and almost every row of it is guessable.

So: guess. Report what was guessed and let the user correct it, rather than
demanding they specify what was never ambiguous.
"""

import re

#: Aliases per field, lower-cased and stripped of punctuation. Ordered
#: longest-first inside each field so "phone number" wins over "number".
_ALIASES = {
    "phone": (
        "phone", "phonenumber", "phoneno", "mobile", "mobileno", "mobilenumber", "whatsapp",
        "whatsappnumber", "contactnumber", "contactno", "number", "msisdn", "cell", "cellphone", "tel",
        "telephone", "mob",
    ),
    "name": (
        "name", "fullname", "customername", "contactname", "clientname", "firstname", "person", "leadname",
    ),
    "email": ("email", "emailaddress", "mail", "emailid"),
    "company": ("company", "companyname", "organisation", "organization", "firm", "business", "shop", "brand"),
    "country": ("country", "countrycode", "nation"),
}

#: Fields the importer writes to columns of their own. Everything else on a
#: row is kept in `custom_fields` rather than dropped -- a column somebody
#: exported is a column somebody cared about.
KNOWN_FIELDS = tuple(_ALIASES)


def _normalise(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


def detect_columns(headers) -> dict:
    """
    Map CSV headers to contact fields.

    Returns ``{field: header}`` for every field that was recognised. The first
    matching header wins, so a file with both "Name" and "First Name" uses
    "Name" -- the one that appeared first is the one the exporter considered
    primary.
    """
    mapping = {}
    normalised = [(header, _normalise(header)) for header in headers or []]

    for field, aliases in _ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            match = next((header for header, norm in normalised if norm == alias), None)
            if match is not None and match not in mapping.values():
                mapping[field] = match
                break
        if field in mapping:
            continue
        # Nothing matched exactly. Fall back to a header that *contains* the
        # alias ("primary mobile no"), which is common and still unambiguous.
        for alias in sorted(aliases, key=len, reverse=True):
            if len(alias) < 4:
                continue
            match = next((header for header, norm in normalised if alias in norm), None)
            if match is not None and match not in mapping.values():
                mapping[field] = match
                break
    return mapping


def row_value(row: dict, mapping: dict, field: str) -> str:
    header = mapping.get(field)
    if not header:
        return ""
    return (row.get(header) or "").strip()


def custom_fields_for(row: dict, mapping: dict) -> dict:
    """Everything on the row that is not one of the mapped columns."""
    used = set(mapping.values())
    return {
        key.strip(): value
        for key, value in row.items()
        if key and key not in used and value and str(value).strip()
    }
