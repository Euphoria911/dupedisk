"""Human-readable size formatting and parsing (binary units)."""

from __future__ import annotations

import re

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*$", re.IGNORECASE)
_SHIFT = {"": 0, "k": 10, "m": 20, "g": 30, "t": 40}


def human(n: int) -> str:
    size = float(n)
    for unit in _UNITS[:-1]:
        if size < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {_UNITS[-1]}"


def parse_size(text: str) -> int:
    """Parse ``512``, ``10K``, ``1.5M``, ``2GiB`` ... into bytes (1K = 1024)."""
    m = _SIZE_RE.match(text)
    if not m:
        raise ValueError(f"invalid size: {text!r}")
    number, suffix = m.groups()
    return int(float(number) * (1 << _SHIFT[suffix.lower()]))
