"""Adapter for `mb_mymeeting_it_v1` (Market_Briefing_2026-10-29_MCP_TEST.json).

Section 4.3. Two things make this format awkward and both are handled here
rather than downstream: segment broadcast times are interpolated between
chapter anchors, and one published chapter time (`Chiediamolo all'AI`, 25:02)
disagrees with the transcript by about 298 s. Chapters are therefore assigned
from the per-segment `rubrica` name, not by time.
"""

from __future__ import annotations

from typing import Any

from ..config import ChapterAssignment, Settings
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
from ..config import INGEST_SCHEMA_VERSION
from ..models.source_it_v1 import ItV1Document
from .common import chapter_at_time, chapter_id_for, derive_end_times, zero_duration_anomalies
from .slugs import normalize, slugify
from .timecodes import parse_timecode_annotated

SOURCE_FORMAT = "mb_mymeeting_it_v1"

# Live-level fields consumed into canonical fields; the rest go to source_meta.
_MAPPED_FIELDS = frozenset(
    {"data", "titolo", "programma", "contenuto_incompleto", "capitoli_ufficiali",
     "titoli_e_strumenti", "segmenti", "speaker_ufficiali"}
)


class AdapterError(ValueError):
    """Raised when the source contradicts what the adapter requires."""


def _live_id(document: ItV1Document) -> str:
    """The source has no id, so it is built from the date: `MB20261029`."""
    digits = document.data.replace("-", "")
    if not digits.isdigit() or len(digits) != 8:
        raise AdapterError(f"cannot build a live id from data={document.data!r}")
    return f"MB{digits}"


def _segment_id(live_id: str, raw_id: int) -> str:
    return f"{live_id}-{raw_id:03d}"


def _build_chapters(document: ItV1Document, live_id: str) -> tuple[list[Chapter], list[str]]:
    """Build chapters, marking any whose published time carried an annotation."""
    official_speakers = {normalize(name) for name in document.speaker_ufficiali}
    chapters: list[Chapter] = []
    notes: list[str] = []

    for index, raw in enumerate(document.capitoli_ufficiali):
        start_s, annotation = parse_timecode_annotated(raw.timestamp_youtube_ufficiale)
        if annotation is not None:
            notes.append(
                f"{live_id}: chapter {raw.nome!r} published time "
                f"{raw.timestamp_youtube_ufficiale!r} carries the annotation {annotation!r}; "
                "recorded as chapter_official_unverified"
            )

        is_guest = normalize(raw.nome) in official_speakers
        chapters.append(
            Chapter(
                chapter_id=chapter_id_for(live_id, index),
                start_s=start_s,
                start_quality=(
                    "chapter_official_unverified" if annotation is not None else "chapter_official"
                ),
                label_raw=raw.nome,
                rubric_id=None if is_guest else slugify(raw.nome),
                speaker_ids=[slugify(raw.nome)] if is_guest else [],
                topics_raw=raw.tema,
            )
        )

    return chapters, notes


def _build_segments(
    document: ItV1Document,
    live_id: str,
    chapters: list[Chapter],
    content_end_s: int,
    assignment: ChapterAssignment,
) -> tuple[list[Segment], list[Anomaly]]:
    """Build segments and record chapter-assignment conflicts.

    Both assignments are always computed so the disagreement is recorded as an
    anomaly whichever one is in force; `assignment` only decides which lands in
    `segment.chapter_id` (section 4.3).
    """
    by_rubric_name = {normalize(chapter.label_raw): chapter.chapter_id for chapter in chapters}

    starts = [raw.time_youtube_estimated_seconds for raw in document.segmenti]
    ends = derive_end_times(starts, content_end_s)

    segments: list[Segment] = []
    conflicts: list[Anomaly] = []

    for raw, start_s, end_s in zip(document.segmenti, starts, ends):
        segment_id = _segment_id(live_id, raw.id)

        rubric_chapter = by_rubric_name.get(normalize(raw.rubrica))
        if rubric_chapter is None:
            raise AdapterError(
                f"segment {segment_id}: rubrica {raw.rubrica!r} matches no chapter name; "
                f"known chapters: {sorted(c.label_raw for c in chapters)}"
            )
        time_chapter = chapter_at_time(chapters, start_s)

        if rubric_chapter != time_chapter:
            conflicts.append(
                Anomaly(
                    kind="chapter_assignment_conflict",
                    segment_ids=[segment_id],
                    chapter_id=rubric_chapter if assignment == "rubrica" else time_chapter,
                    detail=(
                        f"rubrica {raw.rubrica!r} places this segment in {rubric_chapter}, "
                        f"its interpolated start {start_s} s places it in {time_chapter}; "
                        f"assignment in force: {assignment}"
                    ),
                )
            )

        segments.append(
            Segment(
                segment_id=segment_id,
                start_s=start_s,
                end_s=end_s,
                start_quality="estimated_interpolated",
                chapter_id=rubric_chapter if assignment == "rubrica" else time_chapter,
                text=raw.testo,
                speaker=SpeakerAttribution(
                    speaker_id=slugify(raw.speaker),
                    confidence=raw.speaker_confidence,
                    needs_audio_review=raw.requires_audio_check,
                ),
                mixed_speakers=None,
                source={
                    "time_recording": raw.time_recording,
                    "time_recording_seconds": raw.time_recording_seconds,
                    "speaker_originale": raw.speaker_originale,
                    "rubrica": raw.rubrica,
                },
            )
        )

    return segments, conflicts


def _build_mentions(document: ItV1Document, live_id: str, known_segment_ids: set[str]) -> list[Mention]:
    mentions: list[Mention] = []
    for instrument in document.titoli_e_strumenti:
        instrument_id = slugify(instrument.nome)
        for raw_id in instrument.segmenti_ids:
            segment_id = _segment_id(live_id, raw_id)
            if segment_id not in known_segment_ids:
                raise AdapterError(
                    f"titoli_e_strumenti entry {instrument.nome!r} references unknown segment {raw_id}"
                )
            mentions.append(
                Mention(instrument_id=instrument_id, segment_id=segment_id, origin="source_index")
            )
    return mentions


def adapt(
    document: ItV1Document, source_file: str, settings: Settings | None = None
) -> tuple[CanonicalLive, list[str]]:
    """Convert a parsed it_v1 document to canonical form."""
    assignment: ChapterAssignment = (
        settings.chapter_assignment_it_v1 if settings is not None else "rubrica"
    )

    live_id = _live_id(document)
    chapters, notes = _build_chapters(document, live_id)

    # Section 4.3: the broadcast end is unknown, so content ends at the last
    # available segment and duration_seconds stays null.
    content_end_s = document.segmenti[-1].time_youtube_estimated_seconds

    segments, conflicts = _build_segments(document, live_id, chapters, content_end_s, assignment)

    notes.append(
        f"{live_id}: content_end_s ({content_end_s} s) is the last segment's own start, so that "
        "segment is zero-length because the broadcast end is unknown. Reported through "
        "coverage.truncated, not counted as a zero_duration anomaly."
    )
    if conflicts:
        notes.append(
            f"{live_id}: {len(conflicts)} segments where rubrica-based and time-based chapter "
            f"assignment disagree; assignment in force is {assignment!r} "
            "(EASYGOING_CHAPTER_ASSIGNMENT_IT_V1 switches it)"
        )
    mentions = _build_mentions(document, live_id, {s.segment_id for s in segments})

    anomalies = zero_duration_anomalies(
        [s.segment_id for s in segments], [s.start_s for s in segments], [s.end_s for s in segments]
    )
    anomalies.extend(conflicts)
    # Section 4.3: each declared limitation becomes a live-level anomaly.
    anomalies.extend(
        Anomaly(kind="source_limitation", detail=limitation) for limitation in document.limiti
    )

    source_meta: dict[str, Any] = {
        key: value
        for key, value in document.model_dump(by_alias=True).items()
        if key not in _MAPPED_FIELDS
    }

    live = Live(
        live_id=live_id,
        date=document.data,
        title=document.titolo,
        programme_id=slugify(document.programma),
        # No video URL in this source, so no deep links can be built (section 4.6).
        video_url=None,
        source_format=SOURCE_FORMAT,
        source_file=source_file,
        duration_seconds=None,
        ingest_schema_version=INGEST_SCHEMA_VERSION,
        source_meta=source_meta,
    )

    canonical = CanonicalLive(
        live=live,
        coverage=Coverage(
            content_end_s=content_end_s,
            truncated=document.contenuto_incompleto,
            mention_index_scope="multi_asset",
            timestamps_kind="estimated_interpolated",
        ),
        chapters=chapters,
        segments=segments,
        mentions=mentions,
        anomalies=anomalies,
    )
    return canonical, notes
