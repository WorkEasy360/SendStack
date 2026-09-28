"""
Reading a contact file row by row, whichever spreadsheet it came from.

Two formats, one contract: ``open_rows`` hands back the header list and an
iterator of ``{header: text}`` dicts, and neither the preview nor the import
task knows or cares which format produced them. Everything format-specific --
byte-order marks, incremental decoding, Excel's habit of storing every number
as a float -- is settled here, once.

CSV is streamed. A 500,000-row export is a few hundred megabytes, and holding
it twice (raw bytes plus the decoded copy) in a worker that has other tenants'
imports to run is exactly the shape that gets a worker OOM-killed part-way
through somebody's list. Excel is read with openpyxl's read-only mode, which
walks the sheet's XML forward rather than building every cell in memory, so a
large workbook costs about what a large CSV does.

``.xls`` -- the pre-2007 binary format -- is deliberately not read. openpyxl
cannot, the library that can is unmaintained, and every spreadsheet program
made this century saves as ``.xlsx`` in one click. The error names the fix.
"""

import codecs
import csv
import datetime as dt
import os

#: What the importer accepts, and the whole of what it accepts.
SUPPORTED_EXTENSIONS = (".csv", ".xlsx")

#: Bytes pulled off an uploaded CSV at a time while it is being parsed.
_READ_CHUNK_BYTES = 64 * 1024


class UnreadableFile(ValueError):
    """The file could not be read as a spreadsheet. The message is written for the person who uploaded it."""


def extension_of(name) -> str:
    """The lower-cased extension of a filename, or "" -- never anything else."""
    return os.path.splitext(name or "")[1].lower()


def _decoded_lines(field_file):
    """
    Yield an uploaded CSV one line at a time, decoded, without holding it all.

    Django's ``File`` already iterates in chunks; what it does not do is
    decode. So this decodes incrementally, holding one chunk plus one partial
    line. ``incrementaldecoder`` rather than ``chunk.decode()`` because a
    multi-byte character straddling a chunk boundary would otherwise raise --
    a plausible thing to happen in a contact list of names.

    ``utf-8-sig`` is kept: a file saved by Excel begins with a byte-order
    mark, and without stripping it the first column is never recognised.
    """
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    remainder = ""

    field_file.open("rb")
    try:
        for chunk in field_file.chunks(chunk_size=_READ_CHUNK_BYTES):
            try:
                text = decoder.decode(chunk)
            except UnicodeDecodeError as exc:
                # Raised here, mid-stream, rather than up front: the file is
                # never held whole, so the first undecodable byte may be a
                # megabyte in. One exception type for every unreadable file
                # keeps the callers' handling to a single clause.
                raise UnreadableFile("That file is not UTF-8 text. Re-export it as CSV UTF-8.") from exc
            if not text:
                continue
            remainder += text
            # Split on the newline character only, and deliberately not with
            # str.splitlines(): that also breaks on the vertical tab, the form
            # feed, the file/group/record separators and the Unicode line and
            # paragraph separators. The csv module treats none of those as a
            # row terminator, so splitting on them would tear a contact whose
            # name happened to contain one into two rows. A CRLF file needs
            # nothing extra here -- csv strips the trailing carriage return.
            pieces = remainder.split("\n")
            # The last piece carries no terminator, so it is either half a
            # line or empty. Either way it is held over for the next chunk.
            remainder = pieces.pop()
            for piece in pieces:
                yield piece + "\n"
        try:
            tail = decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            # A multi-byte sequence cut off by the end of the file.
            raise UnreadableFile("That file is not UTF-8 text. Re-export it as CSV UTF-8.") from exc
        if tail:
            remainder += tail
        # A final line with no trailing newline is still a row.
        if remainder:
            yield remainder
    finally:
        field_file.close()


def _cell_text(value) -> str:
    """
    One Excel cell as the text a CSV would have carried.

    Excel has no integer type: a phone number typed as digits comes back as
    ``919876543210.0``, and past fifteen digits it has already been rounded
    by Excel itself, which no reader can undo. An integral float is printed
    as an integer so the number survives; anything with a fraction keeps it.
    Dates are ISO 8601, because "12/03/2026" means two different days on two
    sides of an ocean and a custom field should not have to guess.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e17:
            return str(int(value))
        return repr(value)
    if isinstance(value, dt.datetime):
        # Excel has no date type, only datetimes: a cell somebody typed as
        # "2 Jan 2026" comes back at midnight. Print it as the day they typed.
        if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    return str(value).strip()


#: How many non-empty rows are examined for the header before the first of
#: them is taken to be it.
_HEADER_SEARCH_ROWS = 10


def _xlsx_rows(field_file):
    """
    Yield the header list first, then each data row as a dict keyed by header.

    The header row is the first row that names a phone column (see
    ``mapping.detect_columns``), looked for within the first few non-empty
    rows. People leave a title line above their table -- "Customer list
    export" -- and it must not become the column names; a title line is
    non-empty, so "first non-empty row" is not good enough. Rows above the
    header are dropped. If nothing in the first rows names a phone column,
    the first non-empty row is the header after all, and the import fails
    later with the same "no phone column" message a CSV would get.

    Blank rows in the body are skipped for the same reason the CSV reader
    skips them: a gap in a list is not a contact.

    Only the first worksheet is read. A workbook with a second sheet almost
    always has it as notes or a lookup table, and importing it as contacts
    would be wrong far more often than right.
    """
    import openpyxl

    from .mapping import detect_columns

    field_file.open("rb")
    try:
        try:
            workbook = openpyxl.load_workbook(field_file, read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 - openpyxl raises a zoo of types for "not a workbook"
            raise UnreadableFile(
                "That file could not be read as an Excel workbook. Save it as .xlsx and try again."
            ) from exc
        try:
            headers = None
            buffered = []  # non-empty rows seen before the header was decided

            def as_row(cells):
                return {
                    header: (cells[index] if index < len(cells) else "")
                    for index, header in enumerate(headers)
                    if header
                }

            sheet = workbook.worksheets[0] if workbook.worksheets else None
            rows = sheet.iter_rows(values_only=True) if sheet is not None else iter(())
            while True:
                try:
                    values = next(rows)
                except StopIteration:
                    break
                except Exception as exc:  # noqa: BLE001 - a truncated or tampered archive fails part-way through
                    raise UnreadableFile(
                        "That workbook could not be read all the way through. Save it again as .xlsx and retry."
                    ) from exc
                cells = [_cell_text(value) for value in values]
                if not any(cells):
                    continue
                if headers is None:
                    buffered.append(cells)
                    if "phone" in detect_columns(cells):
                        # Everything above the header was a title or a note.
                        headers = cells
                        buffered = []
                        yield [header for header in headers if header]
                    elif len(buffered) >= _HEADER_SEARCH_ROWS:
                        headers = buffered[0]
                        yield [header for header in headers if header]
                        for row in buffered[1:]:
                            yield as_row(row)
                        buffered = []
                    continue
                yield as_row(cells)
            if headers is None:
                if not buffered:
                    yield []
                    return
                headers = buffered[0]
                yield [header for header in headers if header]
                for row in buffered[1:]:
                    yield as_row(row)
        finally:
            workbook.close()
    finally:
        field_file.close()


def open_rows(field_file, name):
    """
    ``(headers, rows)`` for an uploaded contact file.

    ``rows`` is an iterator of ``{header: text}`` dicts with every value
    already a string, so the callers' column detection and phone
    normalisation work identically on both formats. Everything that makes a
    file unreadable -- the wrong format, a CSV that is not UTF-8, a workbook
    that is not really one -- raises ``UnreadableFile`` with advice written
    for the person who uploaded it, either here or part-way through ``rows``.
    """
    extension = extension_of(name)
    if extension == ".xlsx":
        rows = _xlsx_rows(field_file)
        headers = next(rows, [])
        return headers, rows
    if extension == ".csv":
        reader = csv.DictReader(_decoded_lines(field_file))
        return list(reader.fieldnames or []), reader
    if extension == ".xls":
        raise UnreadableFile("Old .xls workbooks cannot be read. Open the file in Excel and save it as .xlsx.")
    raise UnreadableFile("Only .csv and .xlsx files can be imported.")
