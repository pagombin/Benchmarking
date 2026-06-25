"""Small shared helpers for the web app."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """UTC now, second precision, ISO-8601 with trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
