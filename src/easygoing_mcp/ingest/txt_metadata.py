"""Live-level metadata for a `transcript_txt_v1` file.

Resolution order for each field (spec section 2):

1. the `---` metadata block at the top of the .txt;
2. a line form in the .txt (`Video:`, `OSPITI…:`, a `~` on the timeline clock);
3. TEMPORARY: the companion JSON source for the same programme and date in
   `data/source/`, converted with its existing adapter. This exists because the
   first two .txt files carry no metadata block (decided 2026-11-03); new files
   are expected to fill the block instead. Every borrowed field is recorded in
   `fallback` so the canonical file says where it came from.

A required field with no source is an error, never a default. When the .txt and
the companion both state a field and disagree, the .txt wins and a warning is
raised, because the .txt is the input under test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

from ..config import Settings
from ..models.canonical import CanonicalLive, MentionIndexScope, TimestampsKind
from ..models.source_en_v0 import EnV0Document
from ..models.source_it_v1 import ItV1Document
from ..models.source_txt_v1 import TxtDocument
from . import mb_mymeeting_en_v0 as en_v0
from . import mb_mymeeting_it_v1 as it_v1
from .detect import detect_format
from .timecodes import parse_timecode

_SCOPES = frozenset(get_args(MentionIndexScope))
_TIMESTAMP_KINDS = frozenset(get_args(TimestampsKind))


class MetadataError(ValueError):
    """A required live-level field has no source, or the .txt contradicts itself."""


@dataclass
class ResolvedMetadata:
    title: str | None
    video_url: str | None
    content_end_s: int
    truncated: bool
    mention_index_scope: str
    timestamps_kind: str
    guests: list[str]
    origins: dict[str, str] = field(default_factory=dict)
    """Field -> where its value came from (`metadata_block`, `video_line`, …)."""

    fallback: dict[str, str] = field(default_factory=dict)
    """Field -> companion JSON file name, for each field borrowed from it."""

    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def find_companion(txt_name: str, source_dir: Path) -> Path | None:
    """The JSON source sharing the .txt's `<Programme>_<date>_` prefix, if exactly one."""
    stem_parts = Path(txt_name).stem.split("_")
    # `<Programme_Name>_<YYYY-MM-DD>_…`: the prefix runs up to and including the date.
    date_index = next(
        (i for i, part in enumerate(stem_parts) if len(part) == 10 and part[4] == "-" and part[7] == "-"),
        None,
    )
    if date_index is None:
        return None
    prefix = "_".join(stem_parts[: date_index + 1]) + "_"
    matches = sorted(p for p in source_dir.glob(f"{prefix}*.json"))
    if len(matches) > 1:
        raise MetadataError(
            f"{txt_name}: several companion JSON sources match {prefix}*.json: "
            f"{[p.name for p in matches]}; cannot choose one"
        )
    return matches[0] if matches else None


def _companion_canonical(path: Path, settings: Settings | None) -> CanonicalLive:
    raw = json.loads(path.read_text(encoding="utf-8"))
    source_format = detect_format(raw)
    if source_format == "mb_mymeeting_en_v0":
        canonical, _ = en_v0.adapt(EnV0Document.model_validate(raw), path.name)
    else:
        canonical, _ = it_v1.adapt(ItV1Document.model_validate(raw), path.name, settings)
    return canonical


def resolve_metadata(
    document: TxtDocument, source_dir: Path | None, settings: Settings | None = None
) -> ResolvedMetadata:
    block = document.metadata
    name = document.source_file

    # -- what the .txt itself states ----------------------------------------
    stated: dict[str, tuple[Any, str]] = {}

    if block.title is not None:
        stated["title"] = (block.title, "metadata_block")

    if block.video_url is not None and document.video_url is not None and block.video_url != document.video_url:
        raise MetadataError(
            f"{name}: video_url is {block.video_url!r} in the metadata block but "
            f"{document.video_url!r} on the Video: line"
        )
    if block.video_url is not None:
        stated["video_url"] = (block.video_url, "metadata_block")
    elif document.video_url is not None:
        stated["video_url"] = (document.video_url, "video_line")

    if block.content_end is not None:
        stated["content_end_s"] = (parse_timecode(block.content_end), "metadata_block")
    if block.truncated is not None:
        stated["truncated"] = (block.truncated, "metadata_block")

    if block.mention_index_scope is not None:
        if block.mention_index_scope not in _SCOPES:
            raise MetadataError(
                f"{name}: mention_index_scope must be one of {sorted(_SCOPES)}, "
                f"got {block.mention_index_scope!r}"
            )
        stated["mention_index_scope"] = (block.mention_index_scope, "metadata_block")

    estimated = document.segments[0].estimated  # the parser enforces one value per file
    if block.timestamps is not None:
        if block.timestamps not in _TIMESTAMP_KINDS:
            raise MetadataError(
                f"{name}: timestamps must be one of {sorted(_TIMESTAMP_KINDS)}, got {block.timestamps!r}"
            )
        if estimated and block.timestamps != "estimated_interpolated":
            raise MetadataError(
                f"{name}: the metadata block says timestamps: {block.timestamps}, but the "
                "timeline clocks carry `~` (estimated)"
            )
        stated["timestamps_kind"] = (block.timestamps, "metadata_block")
    elif estimated:
        stated["timestamps_kind"] = ("estimated_interpolated", "clock_tilde")

    guests: list[str] = []
    if block.guests is not None and document.guests is not None and block.guests != document.guests:
        raise MetadataError(f"{name}: the guest list differs between the metadata block and the OSPITI line")
    if block.guests is not None:
        guests = list(block.guests)
    elif document.guests is not None:
        guests = list(document.guests)

    # -- the temporary companion-JSON fallback --------------------------------
    companion_values: dict[str, Any] = {}
    companion_name: str | None = None
    if source_dir is not None:
        companion = find_companion(name, source_dir)
        if companion is not None:
            companion_name = companion.name
            canonical = _companion_canonical(companion, settings)
            companion_values = {
                "title": canonical.live.title,
                "video_url": canonical.live.video_url,
                "content_end_s": canonical.coverage.content_end_s,
                "truncated": canonical.coverage.truncated,
                "mention_index_scope": canonical.coverage.mention_index_scope,
                "timestamps_kind": canonical.coverage.timestamps_kind,
            }

    resolved: dict[str, Any] = {}
    origins: dict[str, str] = {}
    fallback: dict[str, str] = {}
    notes: list[str] = []
    warnings: list[str] = []

    for key in ("title", "video_url", "content_end_s", "truncated", "mention_index_scope", "timestamps_kind"):
        if key in stated:
            value, origin = stated[key]
            resolved[key], origins[key] = value, origin
            if key in companion_values and companion_values[key] != value:
                warnings.append(
                    f"{name}: {key} is {value!r} in the .txt ({origin}) but {companion_values[key]!r} "
                    f"in the companion {companion_name}; kept the .txt value"
                )
        elif key in companion_values:
            resolved[key] = companion_values[key]
            origins[key] = f"companion:{companion_name}"
            fallback[key] = companion_name  # type: ignore[assignment]

    # -- fields still missing -------------------------------------------------
    resolved.setdefault("title", None)
    resolved.setdefault("video_url", None)
    for optional in ("title", "video_url"):
        origins.setdefault(optional, "absent")

    missing = [k for k in ("truncated", "mention_index_scope", "timestamps_kind") if k not in resolved]
    if missing:
        raise MetadataError(
            f"{name}: no value for {missing}. State them in the metadata block at the top of the "
            "file (docs/transcript_txt_format.md, section 2)"
            + ("" if companion_name else "; no companion JSON source was found to borrow from")
        )

    if "content_end_s" not in resolved:
        if not resolved["truncated"]:
            raise MetadataError(
                f"{name}: no content_end, and the live is not truncated, so the broadcast end "
                "cannot be derived. State content_end in the metadata block"
            )
        resolved["content_end_s"] = document.segments[-1].start_s
        origins["content_end_s"] = "last_segment_start"
        notes.append(
            f"{name}: content_end_s set to the last segment's start "
            f"({resolved['content_end_s']} s), because the live is truncated and no end is stated"
        )

    if fallback:
        notes.append(
            f"{name}: TEMPORARY fallback — borrowed {sorted(fallback)} from the companion "
            f"{companion_name}; the .txt has no metadata block for them"
        )

    return ResolvedMetadata(
        title=resolved["title"],
        video_url=resolved["video_url"],
        content_end_s=resolved["content_end_s"],
        truncated=resolved["truncated"],
        mention_index_scope=resolved["mention_index_scope"],
        timestamps_kind=resolved["timestamps_kind"],
        guests=guests,
        origins=origins,
        fallback=fallback,
        notes=notes,
        warnings=warnings,
    )
