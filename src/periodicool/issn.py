"""ISSN: the 8-character serial number that identifies a magazine and edition."""

from __future__ import annotations

import re

_FORM = re.compile(r"^\s*(\d{4})\s*-?\s*(\d{3}[\dXx])\s*$")


def check_digit(first7: str) -> str:
    """The eighth character for the first seven digits (mod 11, 10 is X)."""
    total = sum(int(d) * w for d, w in zip(first7, range(8, 1, -1), strict=True))
    value = (11 - total % 11) % 11
    return "X" if value == 10 else str(value)


def valid(code: str) -> bool:
    return normalize(code) is not None


def normalize(raw: str | None) -> str | None:
    """'12345678', '1234-5678' or '1234 567x' to '1234-567X'; None if not a valid ISSN."""
    if not raw:
        return None
    m = _FORM.match(raw)
    if not m:
        return None
    digits = (m.group(1) + m.group(2)).upper()
    if check_digit(digits[:7]) != digits[7]:
        return None
    return f"{digits[:4]}-{digits[4:]}"
