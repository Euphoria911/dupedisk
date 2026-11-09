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


_AGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.IGNORECASE)
_AGE_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_age(text: str) -> float:
    """Parse ``90m``, ``12h``, ``30d``, ``2w`` ... into seconds. A unit is required."""
    m = _AGE_RE.match(text)
    if not m:
        raise ValueError(f"invalid age: {text!r} (use s, m, h, d or w, e.g. 30d)")
    number, unit = m.groups()
    return float(number) * _AGE_SECONDS[unit.lower()]


def parse_size(text: str) -> int:
    """Parse ``512``, ``10K``, ``1.5M``, ``2GiB`` ... into bytes (1K = 1024)."""
    m = _SIZE_RE.match(text)
    if not m:
        raise ValueError(f"invalid size: {text!r}")
    number, suffix = m.groups()
    return int(float(number) * (1 << _SHIFT[suffix.lower()]))
