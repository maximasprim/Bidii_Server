"""
Safe handling of uploaded documents (JDs now; filled appraisal forms later).

* The body is read from the request stream in chunks and aborted the moment it
  exceeds the cap - the whole upload is never buffered first. A declared
  Content-Length over the cap is refused before reading a byte.
* The file type comes from the CONTENT (magic bytes / zip structure), never from
  the client's Content-Type, and the extension must agree with it.
* The client's filename is used only to compare the extension. Nothing is written
  to disk or storage from here and no path is ever built from it.
* Legacy binary .doc is refused with a friendly message.
* DOCX is checked for zip bombs (entry count and total uncompressed size) before parsing.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import AsyncIterable

from fastapi import HTTPException

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_DOCX_UNCOMPRESSED = 40 * 1024 * 1024
MAX_DOCX_ENTRIES = 2000
MAX_PDF_PAGES = 40
MAX_AI_TEXT_CHARS = 12000

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .doc / .xls / .ppt


def _fail(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


async def read_limited(stream: AsyncIterable[bytes], max_bytes: int = MAX_UPLOAD_BYTES, declared_length: str | None = None) -> bytes:
    """Collects the body, aborting as soon as it is larger than `max_bytes`."""
    if declared_length:
        try:
            if int(declared_length) > max_bytes:
                raise _fail(413, f"That file is too large (the limit is {max_bytes // (1024 * 1024)} MB).")
        except ValueError:
            raise _fail(400, "Invalid Content-Length.") from None
    buf = bytearray()
    async for chunk in stream:
        buf += chunk
        if len(buf) > max_bytes:
            raise _fail(413, f"That file is too large (the limit is {max_bytes // (1024 * 1024)} MB).")
    if not buf:
        raise _fail(400, "The file is empty.")
    return bytes(buf)


def _extension(filename: str) -> str:
    name = (filename or "").strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def sniff_kind(data: bytes, filename: str) -> str:
    """'pdf' or 'docx', decided by content. Raises 415 for anything else."""
    head = data[:8]
    if head.startswith(_OLE_MAGIC):
        raise _fail(415, "Legacy Word (.doc) files can't be read safely. Please save it as DOCX or PDF and upload that.")
    if head.startswith(_PDF_MAGIC):
        kind = "pdf"
    elif head.startswith(_ZIP_MAGIC):
        kind = "docx"
    else:
        raise _fail(415, "Unsupported file. Upload a PDF or a DOCX (Word) file.")
    if _extension(filename) != kind:
        raise _fail(415, f"The file's extension doesn't match its content. Rename it to .{kind} or upload the original file.")
    return kind


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise _fail(422, "That PDF is password-protected. Remove the password and upload it again.")
        pages = reader.pages[:MAX_PDF_PAGES]
        return "\n".join((p.extract_text() or "") for p in pages)
    except HTTPException:
        raise
    except Exception:
        raise _fail(422, "That PDF couldn't be read. It may be damaged - try saving it again.") from None


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_DOCX_ENTRIES or sum(i.file_size for i in infos) > MAX_DOCX_UNCOMPRESSED:
                raise _fail(413, "That Word file is too large once unpacked.")
            if "word/document.xml" not in {i.filename for i in infos}:
                raise _fail(415, "That isn't a Word (DOCX) document.")
    except zipfile.BadZipFile:
        raise _fail(415, "That isn't a valid Word (DOCX) document.") from None

    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception:
        raise _fail(422, "That Word file couldn't be read.") from None
    # Walk the body in DOCUMENT ORDER: key responsibilities usually live in a table, and the
    # approvals block that redaction cuts off comes after it.
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    lines: list[str] = []
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            lines.append(Paragraph(child, document).text)
        elif child.tag.endswith("}tbl"):
            for row in Table(child, document).rows:
                seen: set[int] = set()
                cells = []
                for cell in row.cells:
                    if id(cell._tc) in seen:  # merged cells repeat
                        continue
                    seen.add(id(cell._tc))
                    cells.append(cell.text.strip())
                lines.append(" | ".join(c for c in cells if c))
    return "\n".join(lines)


def extract_text(kind: str, data: bytes) -> str:
    text = (_pdf_text(data) if kind == "pdf" else _docx_text(data)).strip()
    if not text:
        raise _fail(
            422,
            "No readable text was found - it looks like a scan or an image. OCR isn't available here, "
            "so please upload a text-based PDF/DOCX or enter the job description manually.",
        )
    return text


# ---- what may leave the building --------------------------------------------

_APPROVAL_HEADING = re.compile(r"^\s*(acceptance|signed by job holder|approvals?)\s*:?\s*$", re.IGNORECASE | re.MULTILINE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# 8+ digit runs (phones, national IDs, account numbers), with optional spaces/dashes. A sentence-ending
# period must not protect a number ("ID 12345678."), but a decimal point followed by a digit does.
_LONG_NUMBER = re.compile(r"(?<!\d)(?<!\d\.)\+?\d[\d\s-]{6,}\d(?!\d)(?!\.\d)")


def redact_for_ai(text: str) -> str:
    """
    A JD's acceptance/approvals block names real people and signatures; contact details
    and ID-like numbers can appear anywhere. Cut the former, mask the latter, and cap the
    length, so nothing personal is sent to the AI provider.
    """
    cut = _APPROVAL_HEADING.search(text)
    if cut:
        text = text[: cut.start()]
    text = _EMAIL.sub("[email]", text)
    text = _LONG_NUMBER.sub("[number]", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_AI_TEXT_CHARS]


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPG_MAGIC = b"\xff\xd8\xff"
EVIDENCE_MAX_BYTES = 5 * 1024 * 1024
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "png": "image/png",
    "jpg": "image/jpeg",
}


def sniff_evidence(data: bytes, filename: str) -> str:
    """'pdf', 'png' or 'jpg' for evidence of a monthly figure (a statement, a report, a photo of a signed sheet). Decided by content."""
    head = data[:8]
    if head.startswith(_PDF_MAGIC):
        kind = "pdf"
    elif head.startswith(_PNG_MAGIC):
        kind = "png"
    elif head.startswith(_JPG_MAGIC):
        kind = "jpg"
    else:
        raise _fail(415, "Evidence must be a PDF, PNG or JPG file.")
    ext = _extension(filename)
    if ext not in ({"jpg", "jpeg"} if kind == "jpg" else {kind}):
        raise _fail(415, f"The file's extension doesn't match its content. Rename it to .{kind} or upload the original file.")
    return kind


def sniff_form_scan(data: bytes, filename: str) -> str | None:
    """'png'/'jpg' when a filled form arrives as a photo or scan; None otherwise. Scans can't be read here - see extraction."""
    try:
        return sniff_evidence(data, filename) if not data.startswith(_PDF_MAGIC) else None
    except HTTPException:
        return None
