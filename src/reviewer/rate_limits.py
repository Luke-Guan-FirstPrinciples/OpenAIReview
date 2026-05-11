"""Small process-local rate limit helpers for external APIs."""

from __future__ import annotations

import os
import threading
import time


_LOCK = threading.Lock()
_LAST_CALL: dict[str, float] = {}


def throttle(key: str, *, min_interval_env: str | None = None, default_seconds: float = 1.0) -> None:
    """Sleep so calls for ``key`` are spaced by at least the configured interval."""
    interval = default_seconds
    if min_interval_env:
        try:
            interval = float(os.environ.get(min_interval_env, interval))
        except ValueError:
            interval = default_seconds
    if interval <= 0:
        return

    with _LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_CALL.get(key, 0.0)
        wait = interval - elapsed
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _LAST_CALL[key] = now
