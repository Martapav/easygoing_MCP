"""Adapter for `transcript_txt_v1`: a parsed .txt transcript to canonical form.

The grammar is in `docs/transcript_txt_format.md`; parsing is in `txt_parse.py`
and live-level metadata in `txt_metadata.py`. This module applies the
format-independent conventions (CLAUDE.md section 4.1) through `common.py`, so
the .txt route cannot drift from the JSON adapters on end times, zero-duration
anomalies or time-based chapter assignment.

Two rules are specific to the .txt route:
- Chapter assignment follows `==== SECTION ====` headers when the file has them
  (the role it_v1's per-segment `rubrica` plays), with every disagreement with
  time-based assignment recorded as `chapter_assignment_conflict`. A file
  without headers is assigned by time.
- The index names instruments but lists no segments, so mentions are found by
  matching each entry's names and tickers in segment text, recorded with
  `origin="index_text_match"`. What the index states (first occurrence, or
  count and speakers) is cross-checked, never trusted or "fixed".
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import INGEST_SCHEMA_VERSION, ChapterAssignment, Settings
from ..models.canonical import (
    Anomaly,
    CanonicalLive,
    Chapter,
    Coverage,
    Live,
    Mention,
    Segment,
    SpeakerAttribution,
)
from ..models.source_txt_v1 import TxtDocument, TxtIndexEntry
from .common import (
    chapter_at_time,
    chapter_id_for,
    derive_end_times,
    split_tickers,
    zero_duration_anomalies,
)
from .slugs import normalize, slugify
from .timecodes import format_timecode, parse_timecode
from .txt_metadata import ResolvedMetadata

SOURCE_FORMAT = "transcript_txt_v1"

# Display-only confidence for a segment without a review flag. Flagged segments
# keep the flag text itself (`?`, `speaker da verificare`) as their confidence.
UNFLAGGED = "unflagged"


class AdapterError(ValueError):
    """Raised when the .txt contradicts what the adapter requires."""


@dataclass
class TxtAdaptResult:
    canonical: CanonicalLive
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# -- identity --------------------------------------------------------------------

def programme_words(source_file: str) -> list[str]:
    """`Market_Briefing_2026-10-28_X.txt` -> `["Market", "Briefing"]`."""
    words: list[str] = []
    for part in Path(source_file).stem.split("_"):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
            return words
        words.append(part)
    raise AdapterError(f"{source_file}: no `_YYYY-MM-DD` in the file name")


def programme_name(source_file: str) -> str:
    return " ".join(programme_words(source_file))


def live_id_for(source_file: str, date: str) -> str:
    """Programme initials + date: `Market_Briefing` on 2026-10-28 -> `MB20261028`."""
    initials = "".join(word[0].upper() for word in programme_words(source_file))
    return f"{initials}{date.replace('-', '')}"


# -- chapters ----------------------------------------------------------------------

def _build_chapters(document: TxtDocument, live_id: str, guests: set[str]) -> list[Chapter]:
    chapters: list[Chapter] = []
    for index, raw in enumerate(document.chapters):
        is_guest = normalize(raw.name) in guests
        chapters.append(
            Chapter(
                chapter_id=chapter_id_for(live_id, index),
                start_s=raw.start_s,
                start_quality=(
                    "chapter_official_unverified" if raw.annotation is not None else "chapter_official"
                ),
                label_raw=raw.label,
                rubric_id=None if is_guest else slugify(raw.name),
                speaker_ids=[slugify(raw.name)] if is_guest else [],
                topics_raw=raw.topics,
            )
        )
    return chapters


def _section_chapters(document: TxtDocument, chapters: list[Chapter]) -> dict[tuple[str, int], str]:
    """(normalized section name, occurrence) -> chapter id.

    The n-th header with a given name is the n-th chapter with that name, so a
    guest who heads two blocks (Sartorelli in MB20261028) maps unambiguously.
    """
    by_name: dict[str, list[str]] = defaultdict(list)
    for raw, chapter in zip(document.chapters, chapters):
        by_name[normalize(raw.name)].append(chapter.chapter_id)

    mapping: dict[tuple[str, int], str] = {}
    for segment in document.segments:
        if segment.section is None or segment.section_occurrence is None:
            continue
        key = (normalize(segment.section), segment.section_occurrence)
        if key in mapping:
            continue
        candidates = by_name.get(key[0], [])
        if len(candidates) < key[1]:
            raise AdapterError(
                f"{document.source_file}:{segment.line}: section header {segment.section!r} "
                f"(occurrence {key[1]}) matches no chapter; chapter names are "
                f"{[raw.name for raw in document.chapters]}"
            )
        mapping[key] = candidates[key[1] - 1]
    return mapping


# -- segments ----------------------------------------------------------------------

def _segment_id(live_id: str, position: int) -> str:
    return f"{live_id}-{position + 1:03d}"


def _build_segments(
    document: TxtDocument,
    live_id: str,
    chapters: list[Chapter],
    meta: ResolvedMetadata,
    assignment: ChapterAssignment,
) -> tuple[list[Segment], list[Anomaly], bool]:
    has_sections = any(s.section is not None for s in document.segments)
    if has_sections:
        orphan = next((s for s in document.segments if s.section is None), None)
        if orphan is not None:
            raise AdapterError(
                f"{document.source_file}:{orphan.line}: segment precedes the first section header; "
                "when a file uses section headers, every segment must sit under one"
            )
    section_map = _section_chapters(document, chapters) if has_sections else {}

    starts = [s.start_s for s in document.segments]
    if meta.content_end_s < starts[-1]:
        raise AdapterError(
            f"{document.source_file}: content_end_s {meta.content_end_s} s "
            f"(from {meta.origins.get('content_end_s')}) precedes the last segment start "
            f"{starts[-1]} s (line {document.segments[-1].line})"
        )
    ends = derive_end_times(starts, meta.content_end_s)

    segments: list[Segment] = []
    conflicts: list[Anomaly] = []
    for position, (raw, start_s, end_s) in enumerate(zip(document.segments, starts, ends)):
        segment_id = _segment_id(live_id, position)
        time_chapter = chapter_at_time(chapters, start_s)
        chapter_id = time_chapter

        if has_sections:
            section_chapter = section_map[(normalize(raw.section), raw.section_occurrence)]  # type: ignore[arg-type]
            chapter_id = section_chapter if assignment == "rubrica" else time_chapter
            if section_chapter != time_chapter:
                conflicts.append(
                    Anomaly(
                        kind="chapter_assignment_conflict",
                        segment_ids=[segment_id],
                        chapter_id=chapter_id,
                        detail=(
                            f"section header {raw.section!r} places this segment in {section_chapter}, "
                            f"its start {start_s} s places it in {time_chapter}; "
                            f"assignment in force: {assignment}"
                        ),
                    )
                )

        source: dict[str, Any] = {"line": raw.line, "clocks": raw.clocks_raw, "review_flag": raw.review_flag}
        if raw.section is not None:
            source["section"] = raw.section
        if raw.other_clocks:
            source["other_clocks"] = dict(raw.other_clocks)
            source["other_clocks_seconds"] = {
                label: parse_timecode(value) for label, value in raw.other_clocks.items()
            }

        segments.append(
            Segment(
                segment_id=segment_id,
                start_s=start_s,
                end_s=end_s,
                start_quality=meta.timestamps_kind,  # type: ignore[arg-type]
                chapter_id=chapter_id,
                text=raw.text,
                speaker=SpeakerAttribution(
                    speaker_id=slugify(raw.speaker),
                    confidence=raw.review_flag if raw.review_flag is not None else UNFLAGGED,
                    needs_audio_review=raw.review_flag is not None,
                ),
                # Never inferred from the text (section 4.1).
                mixed_speakers=None,
                source=source,
            )
        )
    return segments, conflicts, has_sections


# -- mentions ----------------------------------------------------------------------

def name_forms(name: str) -> list[str]:
    """The name, plus each side of a spaced ` / ` (`Alphabet / Google`).

    Only a spaced slash separates alternative names. `Euro/Dollaro` is one
    thing (a currency pair); splitting it would make `euro` match every
    sentence about the currency.
    """
    parts = [part.strip() for part in name.split(" / ") if part.strip()]
    return [name, *parts] if len(parts) > 1 else [name]


def _name_pattern(form: str) -> re.Pattern[str]:
    # Same token-boundary rule as the store's search (store/json_store.py), on
    # normalized text, so `Oro` does not match `lavoro`.
    return re.compile(rf"(?<![0-9a-z]){re.escape(normalize(form))}(?![0-9a-z])")


def _ticker_pattern(ticker: str) -> re.Pattern[str]:
    # Case-sensitive on the raw text: `OR`, `EL`, `V`, `BA` must not hit words.
    return re.compile(rf"(?<![0-9A-Za-z]){re.escape(ticker)}(?![0-9A-Za-z])")


def match_entry(
    entry: TxtIndexEntry, segments: list[Segment], aliases: list[str] | tuple[str, ...] = ()
) -> list[Segment]:
    """Segments in which the entry's name, a name part, an alias or a ticker occurs.

    `aliases` are curated registry aliases for the entry's instrument id. They
    are the only way to add a spoken short form (`Samsung` for `Samsung
    Electronics`); the adapter never invents one.
    """
    forms = [*name_forms(entry.name), *aliases]
    name_patterns = [_name_pattern(form) for form in forms if normalize(form)]
    ticker_patterns = [_ticker_pattern(t) for t in split_tickers(entry.tickers_raw)]
    hits: list[Segment] = []
    for segment in segments:
        folded = normalize(segment.text)
        if any(p.search(folded) for p in name_patterns) or any(p.search(segment.text) for p in ticker_patterns):
            hits.append(segment)
    return hits


def _build_mentions(
    document: TxtDocument,
    segments: list[Segment],
    live_id: str,
    registry_aliases: dict[str, list[str]],
) -> tuple[list[Mention], list[str], list[str]]:
    mentions: list[Mention] = []
    notes: list[str] = []
    warnings: list[str] = []
    seen_ids: dict[str, str] = {}

    for entry in document.index:
        instrument_id = slugify(entry.name)
        if instrument_id in seen_ids:
            raise AdapterError(
                f"{document.source_file}:{entry.line}: index entry {entry.name!r} repeats "
                f"{seen_ids[instrument_id]!r} (same id {instrument_id!r})"
            )
        seen_ids[instrument_id] = entry.name

        hits = match_entry(entry, segments, registry_aliases.get(instrument_id, ()))
        mentions.extend(
            Mention(instrument_id=instrument_id, segment_id=s.segment_id, origin="index_text_match")
            for s in hits
        )

        where = f"{live_id}: index {entry.name!r}"
        if not hits:
            warnings.append(f"{where}: no segment contains its name or tickers")
            continue

        if entry.first_mention_raw is not None:
            stated = parse_timecode(entry.first_mention_raw)
            found = min(s.start_s for s in hits)
            if found != stated:
                warnings.append(
                    f"{where}: first occurrence stated {entry.first_mention_raw} ({stated} s), "
                    f"text match finds {format_timecode(found)} ({found} s)"
                )
        if entry.stated_segment_count is not None and entry.stated_segment_count != len(hits):
            warnings.append(
                f"{where}: stated {entry.stated_segment_count} segments, text match finds {len(hits)}"
            )
        if entry.stated_speakers is not None:
            stated_speakers = {slugify(n) for n in entry.stated_speakers}
            found_speakers = {s.speaker.speaker_id for s in hits}
            if stated_speakers != found_speakers:
                warnings.append(
                    f"{where}: speakers stated {sorted(stated_speakers)}, "
                    f"text match finds {sorted(found_speakers)}"
                )

    notes.append(
        f"{live_id}: {len(mentions)} mentions of {len(seen_ids)} index entries found by text match "
        "(origin index_text_match); the index lists no segments"
    )
    return mentions, notes, warnings


# -- entry point -----------------------------------------------------------------

def adapt(
    document: TxtDocument,
    meta: ResolvedMetadata,
    settings: Settings | None = None,
    registry_aliases: dict[str, list[str]] | None = None,
) -> TxtAdaptResult:
    """Convert a parsed .txt and its resolved metadata to canonical form.

    `registry_aliases` maps instrument id to curated aliases from
    `instruments.json`; they widen mention matching (see `match_entry`).
    """
    assignment: ChapterAssignment = (
        settings.chapter_assignment_txt_v1 if settings is not None else "rubrica"
    )
    live_id = live_id_for(document.source_file, document.date)

    guests = {normalize(n) for n in meta.guests} | {normalize(s.speaker) for s in document.segments}
    chapters = _build_chapters(document, live_id, guests)
    segments, conflicts, has_sections = _build_segments(document, live_id, chapters, meta, assignment)
    mentions, mention_notes, warnings = _build_mentions(
        document, segments, live_id, registry_aliases or {}
    )

    notes = list(meta.notes) + mention_notes
    warnings = list(meta.warnings) + warnings
    if has_sections:
        notes.append(
            f"{live_id}: chapters assigned from section headers; {len(conflicts)} segments where "
            f"that disagrees with time-based assignment (in force: {assignment!r}, "
            "EASYGOING_CHAPTER_ASSIGNMENT_TXT_V1 switches it)"
        )
    else:
        notes.append(f"{live_id}: no section headers; chapters assigned by time")

    anomalies = zero_duration_anomalies(
        [s.segment_id for s in segments], [s.start_s for s in segments], [s.end_s for s in segments]
    )
    anomalies.extend(conflicts)
    anomalies.extend(Anomaly(kind="source_limitation", detail=note) for note in document.notes)

    source_meta: dict[str, Any] = {
        "title_line": document.title_line,
        "header_text": list(document.header_text),
        "metadata_block": document.metadata.model_dump(exclude_none=True),
        "metadata_origin": dict(meta.origins),
        "metadata_fallback": dict(meta.fallback),
        "guests": list(meta.guests),
        "index_form": document.index_form,
        "index_raw": [entry.raw for entry in document.index],
        "notes": list(document.notes),
    }

    live = Live(
        live_id=live_id,
        date=document.date,
        title=meta.title,
        programme_id=slugify(programme_name(document.source_file)),
        video_url=meta.video_url,
        source_format=SOURCE_FORMAT,
        source_file=document.source_file,
        # A truncated live has no known broadcast end, hence no duration.
        duration_seconds=None if meta.truncated else meta.content_end_s,
        ingest_schema_version=INGEST_SCHEMA_VERSION,
        source_meta=source_meta,
    )

    canonical = CanonicalLive(
        live=live,
        coverage=Coverage(
            content_end_s=meta.content_end_s,
            truncated=meta.truncated,
            mention_index_scope=meta.mention_index_scope,  # type: ignore[arg-type]
            timestamps_kind=meta.timestamps_kind,  # type: ignore[arg-type]
        ),
        chapters=chapters,
        segments=segments,
        mentions=mentions,
        anomalies=anomalies,
    )
    return TxtAdaptResult(canonical=canonical, notes=notes, warnings=warnings)
