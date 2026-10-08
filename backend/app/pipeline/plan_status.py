"""Interpret the explicit open/closed flag from the production plan."""
from __future__ import annotations


def closed_status(value: object) -> bool | None:
    """None means that the source did not provide a usable status."""
    normalized = str(value).strip().casefold()
    if normalized in {"1", "1.0", "true"}:
        return True
    if normalized in {"0", "0.0", "false"}:
        return False
    return None
