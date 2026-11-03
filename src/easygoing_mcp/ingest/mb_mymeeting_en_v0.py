"""Adapter for `mb_mymeeting_en_v0` (Market_Briefing_2026-10-28_MCP_TEST.json).

Section 4.2. The source has no live id, title or date field: the id comes from
the segment id prefix, the date from free text cross-checked against the file
name, and there is no title at all (the programme name is not a title).
"""

from __future__ import annotations

import re
from typing import Any

from ..config import INGEST_SCHEMA_VERSION
from ..models.canonical import (
    Anomaly,
    CanonicalLive,
    Chapter,
    Coverage,
    Live,
    Mention,
    Segment,
    SpeakerAttribution,
    StartQuality,
)
from ..models.source_en_v0 import EnV0Document
from .common import chapter_at_time, chapter_id_for, derive_end_times, zero_duration_anomalies
from .slugs import normalize, slugify
from .timecodes import parse_timecode

SOURCE_FORMAT = "mb_mymeeting_en_v0"

# Section 4.2: the source vocabulary for timecode provenance.
_START_QUALITY_BY_TIMECODE_KIND: dict[str, StartQuality] = {
    "raw_asr_chunk_start": "asr_chunk_start",
    "official_chapter": "chapter_official",
}

# `confidence == "review_audio"` is the only value meaning "do not trust this label".
_REVIEW_AUDIO = "review_audio"

_CHAPTER_SEPARATOR = "—"  # em dash, as used in `Left — Right`

_DATE_RE = re.compile(r"\b(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]{3,})\s+(?P<year>\d{4})\b")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Live-level metadata fields consumed into canonical fields; everything else is
# preserved in live.source_meta so nothing is dropped silently (section 4.1).
_MAPPED_METADATA = frozenset({"video", "chapters"})


class AdapterError(ValueError):
    """Raised when the source contradicts what the adapter requires."""


def _parse_source_date(source_text: str) -> str:
    """Pull `28 Oct 2026` out of the free-text `metadata.source` as ISO."""
    match = _DATE_RE.search(source_text)
    if match is None:
        raise AdapterError(
            f"no date of the form '28 Oct 2026' found in metadata.source: {source_text!r}"
        )
    month = _MONTHS.get(match.group("month")[:3].lower())
    if month is None:
        raise AdapterError(f"unrecognised month name in metadata.source: {match.group('month')!r}")
    return f"{int(match.group('year')):04d}-{month:02d}-{int(match.group('day')):02d}"


def _date_from_filename(source_file: str) -> str:
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", source_file)
    if match is None:
        raise AdapterError(f"no ISO date found in the file name {source_file!r}")
    return "-".join(match.groups())


def _programme_id_from_filename(source_file: str) -> str:
    """`Market_Briefing_2026-10-28_MCP_TEST.json` -> `market-briefing`."""
    stem = source_file.split("/")[-1]
    prefix = re.split(r"_\d{4}-\d{2}-\d{2}", stem)[0]
    if not prefix:
        raise AdapterError(f"cannot derive a programme from the file name {source_file!r}")
    return slugify(prefix)


def _live_id(document: EnV0Document) -> str:
    """The shared prefix of the segment ids, e.g. `MB20261028`."""
    prefixes = {segment.id.rsplit("-", 1)[0] for segment in document.segments}
    if len(prefixes) != 1:
        raise AdapterError(f"segments do not share one id prefix: {sorted(prefixes)}")
    prefix = prefixes.pop()
    if not prefix:
        raise AdapterError("empty live id prefix on segment ids")
    return prefix


def _build_chapters(document: EnV0Document, live_id: str) -> list[Chapter]:
    """Classify each chapter label as a guest block or a rubric (section 4.2)."""
    known_speakers = {normalize(segment.speaker) for segment in document.segments}
    chapters: list[Chapter] = []

    for index, raw in enumerate(document.metadata.chapters):
        if _CHAPTER_SEPARATOR in raw.label:
            left, right = raw.label.split(_CHAPTER_SEPARATOR, 1)
            left, topics = left.strip(), right.strip() or None
        else:
            left, topics = raw.label.strip(), None

        is_guest = normalize(left) in known_speakers
        chapters.append(
            Chapter(
                chapter_id=chapter_id_for(live_id, index),
                start_s=parse_timecode(raw.youtube),
                start_quality="chapter_official",
                label_raw=raw.label,
                rubric_id=None if is_guest else slugify(left),
                speaker_ids=[slugify(left)] if is_guest else [],
                topics_raw=topics,
            )
        )

    return chapters


def _build_segments(document: EnV0Document, chapters: list[Chapter], content_end_s: int) -> list[Segment]:
    starts = [parse_timecode(segment.timestamp_youtube) for segment in document.segments]
    ends = derive_end_times(starts, content_end_s)
    segments: list[Segment] = []

    for raw, start_s, end_s in zip(document.segments, starts, ends):
        start_quality = _START_QUALITY_BY_TIMECODE_KIND.get(raw.timecode_kind)
        if start_quality is None:
            raise AdapterError(
                f"segment {raw.id}: unknown timecode_kind {raw.timecode_kind!r}; "
                f"expected one of {sorted(_START_QUALITY_BY_TIMECODE_KIND)}"
            )

        segments.append(
            Segment(
                segment_id=raw.id,
                start_s=start_s,
                end_s=end_s,
                start_quality=start_quality,
                chapter_id=chapter_at_time(chapters, start_s),
                text=raw.text,
                speaker=SpeakerAttribution(
                    speaker_id=slugify(raw.speaker),
                    confidence=raw.confidence,
                    needs_audio_review=raw.confidence == _REVIEW_AUDIO,
                ),
                # Neither source marks this per segment; never inferred (section 4.1).
                mixed_speakers=None,
                source={
                    "timestamp_original": raw.timestamp_original,
                    "timecode_kind": raw.timecode_kind,
                    "source_line": raw.source_line,
                    "speaker_correction": raw.speaker_correction,
                },
            )
        )

    return segments


def _build_mentions(document: EnV0Document, known_segment_ids: set[str]) -> list[Mention]:
    mentions: list[Mention] = []
    for security in document.securities_index:
        instrument_id = slugify(security.name)
        for segment_id in security.segments:
            if segment_id not in known_segment_ids:
                raise AdapterError(
                    f"securities_index entry {security.name!r} references unknown segment {segment_id!r}"
                )
            mentions.append(
                Mention(instrument_id=instrument_id, segment_id=segment_id, origin="source_index")
            )
    return mentions


def adapt(document: EnV0Document, source_file: str) -> tuple[CanonicalLive, list[str]]:
    """Convert a parsed en_v0 document to canonical form.

    Returns the canonical live and a list of notes for the ingest report.
    """
    notes: list[str] = []
    live_id = _live_id(document)

    source_date = _parse_source_date(document.metadata.source)
    filename_date = _date_from_filename(source_file)
    if source_date != filename_date:
        raise AdapterError(
            f"date mismatch for {live_id}: metadata.source says {source_date}, "
            f"file name says {filename_date}"
        )

    content_end_s = parse_timecode(document.metadata.broadcast_content_until_youtube_approx)

    chapters = _build_chapters(document, live_id)
    segments = _build_segments(document, chapters, content_end_s)
    mentions = _build_mentions(document, {segment.segment_id for segment in segments})

    anomalies = zero_duration_anomalies(
        [s.segment_id for s in segments], [s.start_s for s in segments], [s.end_s for s in segments]
    )

    source_meta: dict[str, Any] = {
        key: value
        for key, value in document.metadata.model_dump().items()
        if key not in _MAPPED_METADATA
    }

    # Section 4.2: search_chunks are a derived retrieval chunking with inherited
    # timecodes. Search runs on segments, so they are ignored here by design.
    notes.append(
        f"{live_id}: ignored {len(document.search_chunks)} search_chunks "
        "(derived retrieval chunking with inherited timecodes; search runs on segments)"
    )

    live = Live(
        live_id=live_id,
        date=source_date,
        # The source has no title. The programme name is not a title (section 4.2).
        title=None,
        programme_id=_programme_id_from_filename(source_file),
        video_url=document.metadata.video,
        source_format=SOURCE_FORMAT,
        source_file=source_file,
        duration_seconds=content_end_s,
        ingest_schema_version=INGEST_SCHEMA_VERSION,
        source_meta=source_meta,
    )

    canonical = CanonicalLive(
        live=live,
        coverage=Coverage(
            content_end_s=content_end_s,
            truncated=False,
            mention_index_scope="equities_only",
            timestamps_kind="asr_chunk_start",
        ),
        chapters=chapters,
        segments=segments,
        mentions=mentions,
        anomalies=anomalies,
    )
    return canonical, notes
