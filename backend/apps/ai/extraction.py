"""
Turning an uploaded business document into text worth retrieving from.

Every extractor here returns the same thing: a list of ``Section`` -- a slab
of text plus where in the document it came from. That shape is the whole point
of the module. Retrieval is only useful if an answer can say "Pricing.pdf,
page 4", and page numbers are knowable at extraction time and nowhere else;
recovering them later from a wall of concatenated text is impossible.

What this deliberately does *not* do is OCR. A scanned PDF has no text layer,
and the honest answer is to tell the user their file is images rather than
index nothing and let the agent quietly have no knowledge of it. Parsers are
lazily imported so a deployment missing one dependency loses that one format
with a readable message instead of failing to boot.
"""

import csv
import io
import logging
import os
import re

logger = logging.getLogger("multisender")


class ExtractionError(Exception):
    """Raised when a file cannot be turned into text, with a reason a user can act on."""


class Section:
    """A slab of extracted text and where it came from."""

    __slots__ = ("text", "page", "heading")

    def __init__(self, text, page=None, heading=""):
        self.text = text
        self.page = page
        self.heading = heading

    def __repr__(self):
        return f"Section(page={self.page!r}, heading={self.heading!r}, chars={len(self.text)})"


#: Extension -> a short human name, and the only extensions accepted on upload.
#: An allow-list rather than a block-list: anything not named here is refused
#: before it reaches a parser, which is the cheapest place to stop a file that
#: has no business being parsed at all.
SUPPORTED = {
    ".pdf": "PDF",
    ".docx": "Word document",
    ".txt": "Text file",
    ".md": "Text file",
    ".csv": "Spreadsheet",
    ".xlsx": "Spreadsheet",
}

#: Content types we accept alongside the extension. Browsers and phones are
#: inconsistent about these (a CSV arrives as text/plain, application/csv, or
#: nothing at all), so the extension is authoritative and this is a second
#: opinion rather than a gate.
PLAUSIBLE_TYPES = {
    ".pdf": {"application/pdf", "application/x-pdf", ""},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/octet-stream", "application/zip", "",
    },
    ".txt": {"text/plain", "application/octet-stream", ""},
    ".md": {"text/plain", "text/markdown", "application/octet-stream", ""},
    ".csv": {"text/csv", "application/csv", "text/plain", "application/vnd.ms-excel", "application/octet-stream", ""},
    ".xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/octet-stream", "application/zip", "",
    },
}

MAX_FILE_BYTES = 20 * 1024 * 1024

#: Ceilings that stop one pathological upload from eating a worker. A 5,000
#: page PDF is not a knowledge base, it is a mistake, and the useful response
#: is to index what fits and say so.
MAX_PAGES = 500
MAX_ROWS = 20_000
MAX_TEXT_CHARS = 4_000_000


def extension_of(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def validate_upload(filename: str, size: int, content_type: str = "") -> str:
    """Check a file before it is stored. Returns the extension, or raises."""
    extension = extension_of(filename)
    if extension not in SUPPORTED:
        readable = ", ".join(sorted(SUPPORTED))
        raise ExtractionError(f"{extension or 'That file type'} is not supported. Upload one of: {readable}.")
    if size <= 0:
        raise ExtractionError("That file is empty.")
    if size > MAX_FILE_BYTES:
        raise ExtractionError(f"Files must be smaller than {MAX_FILE_BYTES // (1024 * 1024)}MB.")

    plausible = PLAUSIBLE_TYPES.get(extension, set())
    normalised = (content_type or "").split(";")[0].strip().lower()
    if normalised and plausible and normalised not in plausible:
        # A mismatch is suspicious but not conclusive -- phones send nonsense
        # content types constantly -- so it is logged and the extension wins.
        logger.info("Knowledge upload %s declared content type %s; trusting the extension.", filename, normalised)
    return extension


# ---------------------------------------------------------------------------
# Per-format extractors
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"[ \t ]+")
_BLANKS_RE = re.compile(r"\n{3,}")
#: A line that reads like a heading: short, no terminal punctuation, and either
#: title case or all caps. Crude, and deliberately so -- a wrong guess costs a
#: slightly worse citation, never a wrong answer.
_HEADING_RE = re.compile(r"^[A-Z0-9][^.!?]{2,79}$")


def clean(text: str) -> str:
    """Normalise whitespace without destroying paragraph structure."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = _WHITESPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS_RE.sub("\n\n", text).strip()


def _looks_like_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 80:
        return False
    if not _HEADING_RE.match(line):
        return False
    letters = [character for character in line if character.isalpha()]
    if not letters:
        return False
    return line.isupper() or line.istitle() or sum(c.isupper() for c in letters) / len(letters) > 0.3


def extract_pdf(handle) -> list:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is pinned in requirements
        raise ExtractionError("PDF support is not installed on this server.") from exc

    try:
        reader = PdfReader(handle)
    except Exception as exc:
        raise ExtractionError("That PDF could not be opened. It may be corrupt or password protected.") from exc

    if getattr(reader, "is_encrypted", False):
        # An empty-password decrypt covers the common "protected but not
        # really" case; anything else is a file we genuinely cannot read.
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ExtractionError("That PDF is password protected. Remove the password and upload it again.") from exc

    sections = []
    for index, page in enumerate(reader.pages[:MAX_PAGES], start=1):
        try:
            text = clean(page.extract_text() or "")
        except Exception:
            logger.warning("Could not read page %s of a knowledge PDF; skipping it.", index)
            continue
        if text:
            sections.append(Section(text, page=index))

    if not sections:
        raise ExtractionError(
            "No text could be read from that PDF. If it is a scan, the pages are images rather than text — "
            "paste the text in as a typed document instead."
        )
    return sections


def extract_docx(handle) -> list:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError("Word document support is not installed on this server.") from exc

    try:
        document = docx.Document(handle)
    except Exception as exc:
        raise ExtractionError("That Word document could not be opened. It may be corrupt or an older .doc file.") from exc

    sections = []
    heading = ""
    buffer = []

    def flush():
        body = clean("\n\n".join(buffer))
        if body:
            sections.append(Section(body, heading=heading))
        buffer.clear()

    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        style = (getattr(paragraph.style, "name", "") or "").lower()
        # Word marks its own headings, so unlike a PDF this is not a guess.
        if style.startswith("heading") or style == "title":
            flush()
            heading = text[:200]
            continue
        buffer.append(text)
    flush()

    # Tables are where price lists and specifications actually live, so they
    # are read too rather than silently dropped.
    for table in document.tables:
        rows = []
        for row in table.rows[:MAX_ROWS]:
            cells = [(cell.text or "").strip() for cell in row.cells]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            sections.append(Section(clean("\n".join(rows)), heading="Table"))

    if not sections:
        raise ExtractionError("That Word document has no readable text in it.")
    return sections


def extract_text(handle) -> list:
    raw = handle.read()
    if isinstance(raw, bytes):
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                raw = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ExtractionError("That file is not readable text.")

    body = clean(raw)[:MAX_TEXT_CHARS]
    if not body:
        raise ExtractionError("That file is empty.")

    # Markdown/plain text often carries its own headings; using them keeps
    # citations useful for the format people paste policies into most.
    sections = []
    heading = ""
    buffer = []
    for line in body.split("\n"):
        stripped = line.strip()
        is_heading = stripped.startswith("#") or _looks_like_heading(stripped)
        if is_heading and buffer:
            sections.append(Section(clean("\n".join(buffer)), heading=heading))
            buffer = []
        if is_heading:
            heading = stripped.lstrip("#").strip()[:200]
        else:
            buffer.append(line)
    if buffer:
        sections.append(Section(clean("\n".join(buffer)), heading=heading))

    sections = [section for section in sections if section.text]
    return sections or [Section(body)]


def _rows_to_sections(rows, sheet_name="") -> list:
    """
    Turn tabular rows into sentences a retrieval can match against.

    A raw CSV row ("Premium,4999,Yes") shares no words with "how much is the
    premium plan", so dumping rows verbatim produces a knowledge base that
    cannot answer questions about itself. Pairing each cell with its column
    header ("Plan: Premium. Price: 4999.") is what makes the row findable.
    """
    if not rows:
        return []

    header = [str(cell or "").strip() for cell in rows[0]]
    usable_header = sum(1 for cell in header if cell) >= max(1, len(header) // 2)
    body = rows[1:] if usable_header else rows

    lines = []
    for row in body:
        parts = []
        for index, cell in enumerate(row):
            value = str(cell if cell is not None else "").strip()
            if not value:
                continue
            label = header[index] if usable_header and index < len(header) and header[index] else ""
            parts.append(f"{label}: {value}" if label else value)
        if parts:
            lines.append(". ".join(parts) + ".")

    if not lines:
        return []

    # One section per 40 rows, so a 5,000-row price list becomes findable
    # pieces rather than one unusable slab or 5,000 contextless fragments.
    sections = []
    for start in range(0, len(lines), 40):
        sections.append(Section("\n".join(lines[start : start + 40]), heading=sheet_name))
    return sections


def extract_csv(handle) -> list:
    raw = handle.read()
    if isinstance(raw, bytes):
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                raw = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ExtractionError("That CSV is not readable text.")

    try:
        dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(raw), dialect))[:MAX_ROWS]

    sections = _rows_to_sections(rows)
    if not sections:
        raise ExtractionError("That CSV has no readable rows in it.")
    return sections


def extract_xlsx(handle) -> list:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError("Spreadsheet support is not installed on this server.") from exc

    try:
        # read_only + data_only: we want the values a person sees, not the
        # formulas behind them, and we never want the whole book in memory.
        workbook = load_workbook(handle, read_only=True, data_only=True)
    except Exception as exc:
        raise ExtractionError("That spreadsheet could not be opened. It may be corrupt or an older .xls file.") from exc

    sections = []
    try:
        for sheet in workbook.worksheets:
            rows = []
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index >= MAX_ROWS:
                    break
                rows.append(list(row))
            sections.extend(_rows_to_sections(rows, sheet_name=sheet.title))
    finally:
        workbook.close()

    if not sections:
        raise ExtractionError("That spreadsheet has no readable rows in it.")
    return sections


_EXTRACTORS = {
    ".pdf": extract_pdf,
    ".docx": extract_docx,
    ".txt": extract_text,
    ".md": extract_text,
    ".csv": extract_csv,
    ".xlsx": extract_xlsx,
}


def extract(handle, filename: str) -> list:
    """
    Read `handle` into sections, chosen by `filename`'s extension.

    Any unexpected failure inside a parser becomes an ExtractionError with a
    readable message: a malformed file is a user's problem to fix, not a
    stack trace to show them, and a parser crash must not take the worker with
    it.
    """
    extension = extension_of(filename)
    extractor = _EXTRACTORS.get(extension)
    if extractor is None:
        raise ExtractionError(f"{extension or 'That file type'} is not supported.")

    try:
        sections = extractor(handle)
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 - a parser must never crash the worker
        logger.exception("Knowledge extraction failed for %s", filename)
        raise ExtractionError("That file could not be read. It may be corrupt or in an unexpected format.") from exc

    total = 0
    kept = []
    for section in sections:
        if total >= MAX_TEXT_CHARS:
            break
        section.text = section.text[: MAX_TEXT_CHARS - total]
        total += len(section.text)
        if section.text.strip():
            kept.append(section)
    return kept
