"""
CV text extraction — used ONLY by AI evaluation (app/services/ats_ai_evaluation.py).

The existing weighted-scoring engine (app/services/ats_scoring.py) deliberately
never reads the CV file — see its module docstring. AI evaluation is a
separate, opt-in path where reading the actual CV content is exactly the
point, so this lives in its own module rather than touching that file.

Every failure here (download error, corrupt/unreadable PDF, empty result) is
swallowed and returns None — a candidate whose CV can't be extracted still
gets evaluated on their cover note; see prompts.py's handling of a missing
cv_text. Nothing here ever raises.
"""

import io
import logging

from app.services.storage import BUCKET, supabase

logger = logging.getLogger("bidii.ats_ai.cv_extraction")

MAX_CV_CHARS = 6000


def extract_cv_text(stored_filename: str) -> str | None:
    try:
        data = supabase.storage.from_(BUCKET).download(stored_filename)
    except Exception:
        logger.warning("AI evaluation: couldn't download CV %r from storage for text extraction", stored_filename)
        return None

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages_text = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(pages_text).strip()
    except Exception:
        logger.warning("AI evaluation: couldn't extract text from CV %r", stored_filename)
        return None

    if not text:
        return None
    return text[:MAX_CV_CHARS]
