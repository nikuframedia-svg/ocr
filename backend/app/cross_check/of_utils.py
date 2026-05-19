"""OF normalisation (Round 106).

The OF (ordem de fabrico) is always a 6-digit production-order id. Kanbans
and Excel exports sometimes drop a leading zero — this normaliser pads any
pure-digit OF shorter than 6 to exactly 6 digits.

Values that are not pure digits (e.g. ``05000A`` — handled by ``snap_of``)
or already 6+ digits are returned untouched, so this never fights the
digit-misread recovery in ``dq/snap.py``.
"""
from __future__ import annotations


def normalize_of(value: object) -> str:
    """Return the OF as a 6-digit string when it is a short pure-digit run.

    >>> normalize_of("52306")
    '052306'
    >>> normalize_of("254877")
    '254877'
    >>> normalize_of("05000A")
    '05000A'
    >>> normalize_of("")
    ''
    """
    s = str(value if value is not None else "").strip()
    if not s:
        return ""
    if s.isdigit() and len(s) < 6:
        return s.zfill(6)
    return s
