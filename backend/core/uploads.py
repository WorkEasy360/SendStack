"""
Server-side validation for files accepted through the API.

Everything uploaded here is written under ``MEDIA_ROOT`` and served back by
nginx from ``/media/`` on the application's own origin. That makes an upload a
potential *script* on that origin unless three separate things are true, and
this module is where all three are established:

1. **The extension is on an allowlist.** Not a denylist -- ".html" and ".svg"
   are only two of the dozens of things a browser will happily execute, and a
   list of what the product actually sends is short and knowable.

2. **The bytes agree with the extension.** A browser's ``Content-Type`` and the
   name on the file are both chosen by whoever is uploading, so neither is
   evidence of anything. Every accepted format here has a recognisable
   signature in its first few bytes (or, for plain text, a decodable body with
   no markup at the front), and that signature is what decides. Renaming
   ``payload.html`` to ``photo.png`` gets it rejected, and so does declaring
   ``Content-Type: image/png`` over an HTML body.

3. **The stored path is ours, not theirs.** The name on disk is a fresh UUID
   plus the extension this module derived, so nothing an uploader writes can
   steer where the bytes land. The name they recognise is kept separately, for
   the download label WhatsApp shows.

The stored ``content_type`` is likewise the one derived from the signature, not
the one the browser claimed -- nginx serves by extension, and the value on the
row is what the API reports and what providers are told.
"""

import os
import re
import unicodedata
import uuid

from core.media import DOCUMENT, IMAGE, VIDEO

# Audio has no attachment kind of its own -- WhatsApp takes it, and the
# platform has always carried it as a DOCUMENT (see media_library.classify).
AUDIO_AS = DOCUMENT


class UploadRejected(Exception):
    """An upload failed validation. The message is written to be shown to a user."""


def _starts_with(*prefixes):
    return lambda head: any(head.startswith(prefix) for prefix in prefixes)


def _riff(fourcc):
    # RIFF containers: "RIFF", 4 length bytes, then the form type.
    return lambda head: head[:4] == b"RIFF" and head[8:12] == fourcc


def _isobmff(head):
    # MP4 / MOV / 3GP / M4A all share the ISO base media format: a box length,
    # then the literal "ftyp". The brand that follows distinguishes them, and
    # deliberately isn't checked -- the container is the security-relevant part
    # and brands proliferate faster than any list of them stays correct.
    return head[4:8] == b"ftyp"


def _mpeg_audio(head):
    # An ID3 tag, or a bare MPEG audio frame header (11 set bits).
    if head.startswith(b"ID3"):
        return True
    return len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0


def _plain_text(head):
    """
    Text that is text: decodable, un-terminated, and not markup.

    A NUL byte means this is a binary file wearing a ".txt", and a leading "<"
    means it is a document -- HTML, XML or SVG -- which is the exact thing the
    allowlist exists to keep off this origin.
    """
    if b"\x00" in head:
        return False
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        # A multi-byte character can legitimately straddle the end of the
        # sample; anything else really is not UTF-8.
        try:
            text = head[:-3].decode("utf-8")
        except UnicodeDecodeError:
            return False
    return not text.lstrip().startswith("<")


#: extension -> (content type stored on the row, attachment kind, signature test)
#:
#: Scoped to what the product sends: the image, video, audio and document
#: formats WhatsApp itself accepts, plus the spreadsheet/CSV formats the
#: library is used to hand people. Anything absent is refused, including every
#: format a browser will render as a document.
ALLOWED_UPLOADS = {
    # Images
    ".jpg": ("image/jpeg", IMAGE, _starts_with(b"\xff\xd8\xff")),
    ".jpeg": ("image/jpeg", IMAGE, _starts_with(b"\xff\xd8\xff")),
    ".png": ("image/png", IMAGE, _starts_with(b"\x89PNG\r\n\x1a\n")),
    ".gif": ("image/gif", IMAGE, _starts_with(b"GIF87a", b"GIF89a")),
    ".webp": ("image/webp", IMAGE, _riff(b"WEBP")),

    # Video
    ".mp4": ("video/mp4", VIDEO, _isobmff),
    ".m4v": ("video/mp4", VIDEO, _isobmff),
    ".mov": ("video/quicktime", VIDEO, _isobmff),
    ".3gp": ("video/3gpp", VIDEO, _isobmff),
    ".webm": ("video/webm", VIDEO, _starts_with(b"\x1aE\xdf\xa3")),
    ".mkv": ("video/x-matroska", VIDEO, _starts_with(b"\x1aE\xdf\xa3")),

    # Audio
    ".mp3": ("audio/mpeg", AUDIO_AS, _mpeg_audio),
    ".m4a": ("audio/mp4", AUDIO_AS, _isobmff),
    ".aac": ("audio/aac", AUDIO_AS, _mpeg_audio),
    ".ogg": ("audio/ogg", AUDIO_AS, _starts_with(b"OggS")),
    ".opus": ("audio/ogg", AUDIO_AS, _starts_with(b"OggS")),
    ".wav": ("audio/wav", AUDIO_AS, _riff(b"WAVE")),
    ".amr": ("audio/amr", AUDIO_AS, _starts_with(b"#!AMR")),

    # Documents
    ".pdf": ("application/pdf", DOCUMENT, _starts_with(b"%PDF-")),
    ".doc": ("application/msword", DOCUMENT, _starts_with(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")),
    ".xls": ("application/vnd.ms-excel", DOCUMENT, _starts_with(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")),
    ".ppt": ("application/vnd.ms-powerpoint", DOCUMENT, _starts_with(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")),
    # OOXML files are ZIP containers, so the signature can only prove "a ZIP".
    # That is enough here: the extension decides how it is labelled, and a ZIP
    # is not something a browser executes.
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        DOCUMENT, _starts_with(b"PK\x03\x04", b"PK\x05\x06"),
    ),
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        DOCUMENT, _starts_with(b"PK\x03\x04", b"PK\x05\x06"),
    ),
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        DOCUMENT, _starts_with(b"PK\x03\x04", b"PK\x05\x06"),
    ),
    ".txt": ("text/plain", DOCUMENT, _plain_text),
    ".csv": ("text/csv", DOCUMENT, _plain_text),
    ".md": ("text/plain", DOCUMENT, _plain_text),
}

#: Bytes read from the front of the file to identify it.
SNIFF_BYTES = 4096

#: A stored extension may only ever look like this, whatever arrived.
_SAFE_EXTENSION_RE = re.compile(r"^\.[a-z0-9]{1,10}$")

_UNSAFE_NAME_CHARS = re.compile(r"[\\/\x00-\x1f\x7f]")


def safe_extension(filename):
    """
    The extension of ``filename``, or ``""`` if it is not one we would store.

    Used to build storage paths, so it exists to guarantee that nothing an
    uploader writes -- a slash, a null byte, "..", a second extension -- can
    survive into a path segment.
    """
    extension = os.path.splitext(filename or "")[1].lower()
    return extension if _SAFE_EXTENSION_RE.match(extension) else ""


def safe_display_filename(filename, *, fallback="attachment"):
    """
    The original name, made safe to store and to show.

    This is only ever a *label* -- WhatsApp prints it under a document -- and
    never a path. Path separators and control characters are stripped anyway,
    so that a name cannot become one somewhere downstream.
    """
    name = os.path.basename((filename or "").replace("\\", "/"))
    name = unicodedata.normalize("NFKC", name)
    name = _UNSAFE_NAME_CHARS.sub("", name).strip().strip(".")
    return name[:255] or fallback


def _head_of(upload):
    position = upload.tell() if hasattr(upload, "tell") else 0
    upload.seek(0)
    head = upload.read(SNIFF_BYTES)
    upload.seek(position)
    return head or b""


def validate_upload(upload, *, allowed=None):
    """
    Check one uploaded file and describe how it should be stored.

    Returns ``(extension, content_type, kind, stored_name, display_name)``.
    Raises :class:`UploadRejected` -- whose message names the file and says
    what was wrong with it -- on anything not on the allowlist, anything whose
    bytes disagree with its extension, and anything empty.

    ``allowed`` narrows the allowlist for a caller that accepts less than the
    media library does (the CSV importer, say); it defaults to everything.
    """
    table = ALLOWED_UPLOADS if allowed is None else {ext: ALLOWED_UPLOADS[ext] for ext in allowed}

    display_name = safe_display_filename(getattr(upload, "name", ""))
    extension = safe_extension(getattr(upload, "name", ""))

    if not extension:
        raise UploadRejected(f"{display_name}: files must have a file extension.")
    if extension not in table:
        raise UploadRejected(
            f"{display_name}: {extension} files are not accepted. "
            f"Allowed types: {', '.join(sorted(table))}."
        )

    content_type, kind, matches = table[extension]

    head = _head_of(upload)
    if not head:
        raise UploadRejected(f"{display_name}: the file is empty.")
    if not matches(head):
        # Deliberately the same message for "renamed .html" and "spoofed
        # Content-Type": both are the file not being what it says it is, and
        # a more specific message would only help someone probing the check.
        raise UploadRejected(
            f"{display_name}: the file's contents are not a valid "
            f"{extension.lstrip('.').upper()} file."
        )

    return extension, content_type, kind, f"{uuid.uuid4().hex}{extension}", display_name
