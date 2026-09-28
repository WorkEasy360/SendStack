"""
Smallest files that are genuinely of their type, for tests.

Uploads are validated by their leading bytes (see ``core.uploads``), so a test
can no longer stand in a payload of ``b"png-bytes"`` for a picture -- and it
should not want to: a test that uploads something the product would refuse is
not testing the upload path. These are real signatures with the smallest
plausible body behind them.

Kept out of any ``tests.py`` because several apps' suites need them.
"""

from django.core.files.uploadedfile import SimpleUploadedFile

#: A 1x1 transparent GIF -- the smallest complete image there is.
GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)

#: A 1x1 PNG: signature, IHDR, a minimal IDAT, IEND.
PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

#: JPEG start-of-image plus an APP0/JFIF header and an end-of-image.
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"

#: An ISO base media file: box length, "ftyp", and the isom brand. MP4, MOV,
#: 3GP and M4A are all this container with a different brand.
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41"

#: An EBML header -- what WebM and Matroska both start with.
WEBM = b"\x1aE\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1f"

#: An ID3v2 tag, which is how nearly every MP3 in the world begins.
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 4

#: A PDF that a reader will actually open: one empty page.
PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 72 72]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)

#: An empty ZIP, which is the container every OOXML document is.
ZIP = b"PK\x05\x06" + b"\x00" * 18

CSV = b"phone,name\n+447700900000,Ada\n"

TXT = b"Opening hours are 9 to 5, Monday to Friday.\n"


def upload(name, data=None, content_type=""):
    """
    A ``SimpleUploadedFile`` whose bytes match its extension by default.

    Pass ``data`` to deliberately mismatch them -- which is what the rejection
    tests do. ``content_type`` is whatever a browser would have claimed, and
    the point of most of these tests is that it decides nothing.
    """
    if data is None:
        data = FOR_EXTENSION[name.rsplit(".", 1)[-1].lower()]
    return SimpleUploadedFile(name, data, content_type=content_type)


FOR_EXTENSION = {
    "png": PNG,
    "jpg": JPEG,
    "jpeg": JPEG,
    "gif": GIF,
    "mp4": MP4,
    "m4v": MP4,
    "mov": MP4,
    "3gp": MP4,
    "m4a": MP4,
    "webm": WEBM,
    "mkv": WEBM,
    "mp3": MP3,
    "pdf": PDF,
    "docx": ZIP,
    "xlsx": ZIP,
    "pptx": ZIP,
    "csv": CSV,
    "txt": TXT,
    "md": TXT,
}
