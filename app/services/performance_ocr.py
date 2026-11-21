"""
Reading photographed or scanned forms ON THIS SERVER with Tesseract. No image or text is sent anywhere by this module;
the text it returns then follows the normal route (pseudonymise -> AI mapping -> human review -> confirm).

* Images are opened defensively: pixel and size caps (decompression-bomb protection), verified, rotated by their EXIF
  orientation, flattened to grey and contrast-stretched. Tesseract runs with a hard timeout.
* A scanned PDF has no text layer; its pages are usually one embedded image each, so those images are read (first MAX_PAGES pages).
* The result carries the mean word confidence (0-100). Extraction scales its own confidences by it so a poor scan is flagged, not trusted.
* Handwriting is read poorly by design: every value still has to be checked by a person on the review screen.
"""

from __future__ import annotations

import io
import shutil
from collections import OrderedDict
from dataclasses import dataclass

MAX_PIXELS = 25_000_000
MAX_SIDE = 9000
MIN_SIDE = 1400  # small photos are enlarged so the reader can see the letters
MAX_PAGES = 10
OCR_TIMEOUT_SECONDS = 40
LANG = "eng"


class OcrUnavailable(RuntimeError):
    """Tesseract isn't installed on this server."""


class OcrError(ValueError):
    """The picture couldn't be read; the message is safe to show."""


@dataclass(frozen=True)
class OcrResult:
    text: str
    confidence: float  # mean word confidence, 0-100
    pages: int


def available() -> bool:
    return shutil.which("tesseract") is not None


def _prepare(data: bytes):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()  # structure check; the image must be reopened afterwards
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_PIXELS or max(img.size) > MAX_SIDE:
            raise OcrError("That image is too large. Please scan at a lower resolution (about 200-300 dpi).")
        img = ImageOps.exif_transpose(img)
        img = ImageOps.autocontrast(ImageOps.grayscale(img))
        if max(img.size) < MIN_SIDE:
            scale = MIN_SIDE / max(img.size)
            img = img.resize((int(img.width * scale), int(img.height * scale)))
        return img
    except OcrError:
        raise
    except Exception:  # Pillow raises many types, including DecompressionBombError
        raise OcrError("That image couldn't be opened. It may be damaged - try scanning or photographing it again.") from None


def _read(img) -> tuple[str, list[float]]:
    import pytesseract

    try:
        d = pytesseract.image_to_data(img, lang=LANG, config="--psm 6", output_type=pytesseract.Output.DICT, timeout=OCR_TIMEOUT_SECONDS)
    except RuntimeError:
        raise OcrError("Reading that image took too long. Try a clearer, smaller scan.") from None
    except pytesseract.TesseractNotFoundError:
        raise OcrUnavailable("Reading scans isn't set up on this server.") from None
    lines: dict[tuple[int, int, int], list[str]] = OrderedDict()
    confs: list[float] = []
    for text, conf, b, p, ln in zip(d["text"], d["conf"], d["block_num"], d["par_num"], d["line_num"]):
        if not str(text).strip():
            continue
        lines.setdefault((b, p, ln), []).append(str(text).strip())
        try:
            c = float(conf)
        except (TypeError, ValueError):
            continue
        if c >= 0:
            confs.append(c)
    return "\n".join(" ".join(words) for words in lines.values()), confs


def _combine(pages: list[tuple[str, list[float]]]) -> OcrResult:
    text = "\n\n".join(t for t, _ in pages).strip()
    confs = [c for _, cs in pages for c in cs]
    if not text:
        raise OcrError("No readable text was found in that picture. Please enter the appraisal by hand (the original stays attached).")
    return OcrResult(text=text, confidence=sum(confs) / len(confs) if confs else 0.0, pages=len(pages))


def read_image(data: bytes) -> OcrResult:
    if not available():
        raise OcrUnavailable("Reading scans isn't set up on this server.")
    return _combine([_read(_prepare(data))])


def read_scanned_pdf(data: bytes) -> OcrResult:
    """OCR the images embedded in a PDF's first pages (a scanned PDF has no text layer)."""
    if not available():
        raise OcrUnavailable("Reading scans isn't set up on this server.")
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        pages: list[tuple[str, list[float]]] = []
        for page in reader.pages[:MAX_PAGES]:
            for image in list(page.images)[:3]:
                pages.append(_read(_prepare(image.data)))
    except (OcrError, OcrUnavailable):
        raise
    except Exception:
        raise OcrError("That PDF's pages couldn't be read as pictures. Please upload a clearer scan.") from None
    return _combine(pages)
