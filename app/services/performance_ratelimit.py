"""
Per-user sliding-window rate limits for the AI, upload and extraction endpoints.

In-memory and per process: it stops a runaway script or a double-clicking admin,
which is the threat for these endpoints. If the app is ever run with several
workers each one counts separately (so the effective limit is higher); move the
counters to the database or Redis if that becomes a concern.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import Depends, HTTPException

from app.models.admin_user import AdminUser
from app.services.auth import get_current_admin


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_seconds: int, now: float | None = None) -> int | None:
        """Records a hit. Returns None if allowed, else the seconds until the next hit would be allowed."""
        t = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= t - window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                return max(1, int(hits[0] + window_seconds - t) + 1)
            hits.append(t)
            return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()

# AI, extraction and upload endpoints share one budget per user.
AI_LIMIT = 15
AI_WINDOW_SECONDS = 3600


def rate_limited(bucket: str = "ai", limit: int = AI_LIMIT, window_seconds: int = AI_WINDOW_SECONDS):
    def dependency(current_admin: AdminUser = Depends(get_current_admin)) -> None:
        wait = limiter.check(f"{bucket}:{current_admin.id}", limit, window_seconds)
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail=f"Too many requests. Please try again in about {max(1, wait // 60)} minute(s).",
                headers={"Retry-After": str(wait)},
            )

    return dependency
