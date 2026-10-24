"""
Importing contacts from text somebody pasted in.

The CSV importer already handles the case where a list exists as a file. It
does not handle the far more common one: a WhatsApp message, a spreadsheet
column, or a page of a notebook that somebody has in front of them and wants
to send to. Making them save a CSV first is the reason those lists get typed
into the composer one number at a time instead.

Two formats, because those are the two people actually paste::

    9876543210                  a number per line
    Aman,9876543210             a name and a number

and both tolerate whatever separator the source used (comma, tab, semicolon,
pipe, or an en/em dash), whichever order the two fields are in, and the
quoting a spreadsheet adds when a name contains a comma.

**Nothing here writes.** `parse` classifies every line and returns what it
found; `commit` (in views) is a separate, explicit call. That separation is
the feature, not an implementation detail -- a paste of a hundred numbers with
four typos in it should be reviewed and corrected before anything is created,
and an importer that saved as it parsed would leave the corrections to be made
afterwards against rows that already exist.

Every row keeps its original line, so the review table can put a status next
to the text the person actually pasted rather than next to a normalised
number they will not recognise.
"""

import re

from .utils import apply_country_code, normalize_phone

#: Row statuses, in the order they are checked. Each is a different thing to
#: do about it, which is why they are not collapsed into "bad".
NEW = "NEW"
EXISTING = "EXISTING"
UNSUBSCRIBED = "UNSUBSCRIBED"
DUPLICATE = "DUPLICATE"
INVALID = "INVALID"
NO_COUNTRY_CODE = "NO_COUNTRY_CODE"
EMPTY = "EMPTY"

#: Statuses that must never be imported, whatever the reviewer clicks. The
#: separation matters: DUPLICATE and EXISTING are *skippable*, not wrong, and
#: a reviewer may legitimately want an existing contact's name updated.
UNIMPORTABLE = (INVALID, NO_COUNTRY_CODE, EMPTY)

#: How each status reads in the review table. Sentences live here rather than
#: in the browser so the API and the screen cannot describe a row differently.
STATUS_LABELS = {
    NEW: "New contact",
    EXISTING: "Already in your contacts",
    UNSUBSCRIBED: "Unsubscribed -- will not be messaged",
    DUPLICATE: "Repeated in this paste",
    INVALID: "Not a valid phone number",
    NO_COUNTRY_CODE: "No country code",
    EMPTY: "No number on this line",
}

#: What separates a name from a number on one line. Deliberately not just the
#: comma: people paste tab-separated spreadsheet columns and "Aman - 98765..."
#: as readily as CSV, and rejecting those would send them back to Excel.
_SEPARATORS = re.compile(r"[,;\t|]|\s+[-–—]\s+")

#: A field that is a phone number rather than a name: digits, with the
#: punctuation a written number carries, and nothing else.
_PHONE_LIKE = re.compile(r"^[+()\s\d.\-]+$")

#: Longest a paste may be. Well past any list somebody types or copies out of
#: a chat, and short of the size where this should have been a file -- the CSV
#: importer streams, this one holds every row in memory to show them all.
MAX_LINES = 5000

def _split_line(line: str) -> tuple:
    """
    ``(name, raw_phone)`` for one pasted line.

    Which field is which is decided by which one *looks like a number*, not by
    position: people paste "Aman, 98765 43210" and "98765 43210, Aman" in
    roughly equal measure, and a parser that assumed an order would silently
    import half of one customer's list with the numbers in the name column.
    """
    parts = [part.strip().strip('"').strip("'") for part in _SEPARATORS.split(line)]
    parts = [part for part in parts if part]

    if not parts:
        return "", ""
    if len(parts) == 1:
        single = parts[0]
        return ("", single) if _PHONE_LIKE.match(single) else (single, "")

    # More than two fields (a pasted spreadsheet row) is common. Take the
    # first field that looks like a number as the number, and the first that
    # does not as the name; the rest is whatever else was in the row and is
    # not guessed at.
    phone = next((part for part in parts if _PHONE_LIKE.match(part)), "")
    name = next((part for part in parts if part != phone and not _PHONE_LIKE.match(part)), "")
    return name, phone


def _normalise(raw_phone: str, default_country_code: str) -> tuple:
    """
    ``(phone, status)`` -- the E.164 number, or why there isn't one.

    The country code is the whole difficulty here, and getting it wrong is
    worse than refusing. "9876543210" pasted on its own is a perfectly ordinary
    Indian mobile with the code left off; it is also, read as E.164, a valid
    ten-digit number in country +98 (Iran). Prefixing a default silently would
    be a guess, and *not* prefixing it silently produces a number that passes
    validation and belongs to somebody else.

    So the default country code is something the person pasting states, and a
    number with no code and no stated default is refused by name -- with the
    number shown in the review table either way, so the guess can be seen
    before it is acted on.
    """
    # Which leading digits are a country code and which are the contact's own
    # number is decided in one place for every way a number gets in -- see
    # apply_country_code. A ten-digit 9112345678 used to be read here as +91
    # followed by eight digits, silently storing somebody else's number.
    digits = apply_country_code(raw_phone, default_country_code)
    if digits == "":
        return "", EMPTY
    if digits is None:
        return "", NO_COUNTRY_CODE

    try:
        return normalize_phone(digits), ""
    except ValueError:
        return "", INVALID


def parse(text: str, *, default_country_code: str = "", existing=None, unsubscribed=None) -> list:
    """
    Classify every line of a paste. Writes nothing.

    `existing` and `unsubscribed` are sets of E.164 numbers already on the
    account; the caller looks them up in one query rather than this function
    doing a query per row. Passing neither is valid and yields NEW for
    everything that parses, which is what a pure syntax check wants.
    """
    existing = existing or set()
    unsubscribed = unsubscribed or set()

    rows = []
    seen: set = set()

    for number, line in enumerate((text or "").splitlines(), start=1):
        if len(rows) >= MAX_LINES:
            break
        # A blank line is not an error -- people paste lists with spacing in
        # them -- so it is dropped rather than reported. A line with *content*
        # but no number is reported, because that one is a mistake.
        if not line.strip():
            continue

        name, raw_phone = _split_line(line)
        phone, problem = _normalise(raw_phone, default_country_code)

        if problem:
            status = problem
        elif phone in seen:
            status = DUPLICATE
        elif phone in unsubscribed:
            # Checked before EXISTING: an unsubscribed contact *is* an
            # existing one, and which of the two the reviewer is told about
            # decides whether they understand that importing this row will not
            # make it messageable.
            status = UNSUBSCRIBED
        elif phone in existing:
            status = EXISTING
        else:
            status = NEW

        if phone:
            seen.add(phone)

        rows.append({
            "line": number,
            "raw": line.strip(),
            "name": name,
            "phone": phone,
            "status": status,
            "detail": STATUS_LABELS[status],
            # What the "Import" button will actually do with this row, so the
            # table's checkbox state comes from the server's rules rather than
            # the browser re-deriving them.
            "importable": status not in UNIMPORTABLE,
        })

    return rows


def summarise(rows) -> dict:
    """Counts per status, for the header above the review table."""
    counts = {status: 0 for status in STATUS_LABELS}
    for row in rows:
        counts[row["status"]] += 1
    return {
        "total": len(rows),
        "new": counts[NEW],
        "existing": counts[EXISTING],
        "unsubscribed": counts[UNSUBSCRIBED],
        "duplicates": counts[DUPLICATE],
        "invalid": counts[INVALID],
        "no_country_code": counts[NO_COUNTRY_CODE],
        "empty": counts[EMPTY],
        "importable": sum(1 for row in rows if row["importable"]),
    }
