"""Timecode parsing.

Section 4.2: `timestamp_youtube` is `mm:ss` below one hour and `h:mm:ss` above.
Both forms genuinely occur in MB20261028 (69 and 68 segments respectively), so
one parser handles both. Section 4.3: official it_v1 chapter times may carry a
free-text annotation such as `25:02 [da verificare]`, which is stripped and
reported rather than silently dropped.
"""

from __future__ import annotations

import re

_ANNOTATION_RE = re.compile(r"\s*\[(?P<annotation>[^\]]*)\]\s*")


class TimecodeError(ValueError):
    """Raised for a timecode that does not parse. Never guessed around."""


def parse_timecode(value: str) -> int:
    """Parse `mm:ss` or `h:mm:ss` (also `hh:mm:ss`) into whole seconds.

    Minutes and seconds must each be below 60: `1:75` is a malformed timecode,
    not 135 seconds, and accepting it would silently distort the timeline.
    """
    if not isinstance(value, str):
        raise TimecodeError(f"expected a timecode string, got {type(value).__name__}")

    raw = value.strip()
    if not raw:
        raise TimecodeError("empty timecode")

    parts = raw.split(":")
    if len(parts) == 2:
        hours, (minutes, seconds) = 0, parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise TimecodeError(f"expected mm:ss or h:mm:ss, got {value!r}")

    try:
        h, m, s = int(hours), int(minutes), int(seconds)
    except ValueError as exc:
        raise TimecodeError(f"non-numeric timecode component in {value!r}") from exc

    if m >= 60 or s >= 60 or h < 0 or m < 0 or s < 0:
        raise TimecodeError(f"out-of-range timecode component in {value!r}")

    return h * 3600 + m * 60 + s


def parse_timecode_annotated(value: str) -> tuple[int, str | None]:
    """Parse a timecode that may carry a bracketed annotation.

    Returns `(seconds, annotation)`, where annotation is the bracketed text
    without the brackets, or None when there was none. The caller decides what
    an annotation means; here it is only separated from the digits.
    """
    match = _ANNOTATION_RE.search(value)
    if match is None:
        return parse_timecode(value), None

    annotation = match.group("annotation").strip() or None
    stripped = _ANNOTATION_RE.sub("", value).strip()
    return parse_timecode(stripped), annotation


def format_timecode(seconds: int) -> str:
    """Render whole seconds as `hh:mm:ss` for display labels (section 5)."""
    if seconds < 0:
        raise TimecodeError(f"cannot format negative seconds: {seconds}")
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
