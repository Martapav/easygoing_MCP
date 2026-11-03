"""Source format detection.

An unrecognised file is an error, never a guess: a wrong adapter would produce
plausible-looking canonical data with silently wrong timings (section 2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

from ..models.canonical import SourceFormat

IT_V1_SCHEMA = "easygoing.market_briefing.transcript_test.v1"

# Section 4.2: en_v0 has no schema label, so it is identified by its key signature.
EN_V0_KEYS = frozenset({"metadata", "securities_index", "segments", "search_chunks"})

KNOWN_FORMATS: tuple[str, ...] = get_args(SourceFormat)


class UnknownSourceFormat(ValueError):
    """Raised when no adapter claims a file."""


def detect_format(document: Any) -> SourceFormat:
    """Return the source format of a parsed JSON document.

    Raises `UnknownSourceFormat` with the observed shape when nothing matches,
    so the operator can see why rather than guessing.
    """
    if not isinstance(document, dict):
        raise UnknownSourceFormat(
            f"expected a JSON object at the top level, got {type(document).__name__}"
        )

    schema = document.get("schema")
    if schema == IT_V1_SCHEMA:
        return "mb_mymeeting_it_v1"

    if EN_V0_KEYS.issubset(document.keys()):
        return "mb_mymeeting_en_v0"

    observed = sorted(document.keys())
    raise UnknownSourceFormat(
        "unrecognised source format. "
        f"Known formats: {', '.join(KNOWN_FORMATS)}. "
        f"Expected either schema == {IT_V1_SCHEMA!r} (got {schema!r}) "
        f"or the top-level keys {sorted(EN_V0_KEYS)}. "
        f"Observed top-level keys: {observed}."
    )


TXT_FORMAT = "transcript_txt_v1"


def detect_source(path: Path) -> SourceFormat:
    """Return the source format of a file on disk, from its suffix and content.

    `.json` goes through `detect_format`. `.txt` is claimed by
    `transcript_txt_v1` only if it has a title line and at least one segment line
    (docs/transcript_txt_format.md); the strict parse comes afterwards. Any other
    suffix is an error.
    """
    from .txt_parse import txt_signature  # local import: txt_parse imports this package's siblings

    suffix = path.suffix.lower()
    if suffix == ".json":
        import json

        return detect_format(json.loads(path.read_text(encoding="utf-8")))
    if suffix == ".txt":
        text = path.read_text(encoding="utf-8")
        if txt_signature(text):
            return TXT_FORMAT  # type: ignore[return-value]
        raise UnknownSourceFormat(
            f"{path.name}: not a {TXT_FORMAT} transcript. Expected a title line "
            "`PROGRAMME — DD MESE YYYY` and segment lines `[time] Speaker: text` "
            "(docs/transcript_txt_format.md)."
        )
    raise UnknownSourceFormat(
        f"{path.name}: unsupported file type {suffix!r}. Known formats: {', '.join(KNOWN_FORMATS)} "
        "(.json or .txt)."
    )
