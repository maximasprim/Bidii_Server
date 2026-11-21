"""
Private file storage for the performance module (filled appraisal forms, entry evidence).

Files live in the existing private Supabase bucket under `performance/...`. The stored name is ALWAYS generated here
(a uuid plus an extension taken from the sniffed file type) - nothing the client sends is ever part of a path.
Files leave only through an authorised endpoint as an attachment; no public URL is ever created.
The Supabase client is imported lazily so tests can replace `_client` without touching the network.
"""

from __future__ import annotations

import re
import uuid

PREFIX = "performance"
_SAFE_ID = re.compile(r"^[A-Za-z0-9-]{1,40}$")
_SAFE_EXT = {"pdf", "docx", "png", "jpg"}


class StorageError(RuntimeError):
    pass


def _client():  # pragma: no cover - replaced in tests
    from app.services.storage import BUCKET, supabase

    return supabase.storage.from_(BUCKET)


def new_path(folder: str, owner_id: str, ext: str) -> str:
    """performance/<folder>/<owner id>/<random>.<ext>; rejects anything that is not a plain id or a known extension."""
    if folder not in ("appraisals", "evidence", "packs") or not _SAFE_ID.match(owner_id or "") or ext not in _SAFE_EXT:
        raise StorageError("Invalid storage location.")
    return f"{PREFIX}/{folder}/{owner_id}/{uuid.uuid4().hex}.{ext}"


def is_ours(path: str | None) -> bool:
    """True only for a path this module could have generated (blocks traversal and foreign keys)."""
    return bool(path) and re.fullmatch(rf"{PREFIX}/(appraisals|evidence|packs)/[A-Za-z0-9-]{{1,40}}/[0-9a-f]{{32}}\.(pdf|docx|png|jpg)", path or "") is not None


def put(path: str, data: bytes, content_type: str) -> None:
    if not is_ours(path):
        raise StorageError("Invalid storage location.")
    try:
        _client().upload(path, data, {"content-type": content_type, "upsert": "false"})
    except Exception as exc:  # the SDK raises several types
        raise StorageError("The file couldn't be saved. Please try again.") from exc


def get(path: str) -> bytes:
    if not is_ours(path):
        raise StorageError("Invalid storage location.")
    try:
        return bytes(_client().download(path))
    except Exception as exc:
        raise StorageError("The file couldn't be read.") from exc


def remove(path: str) -> None:
    if not is_ours(path):
        return
    try:
        _client().remove([path])
    except Exception:  # best effort: the record is what matters
        return
