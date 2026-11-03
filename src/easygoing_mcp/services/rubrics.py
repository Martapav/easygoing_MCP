"""Rubric resolution and occurrences (REQ-0017)."""

from __future__ import annotations

import difflib

from ..ingest.slugs import normalize, slugify
from ..models.outputs import RubricOccurrence, RubricOccurrencesResult
from ..models.registries import RubricEntry
from ..store.base import ContentStore, UnknownRubric
from .lives import live_caveats, timestamp_label


def resolve_rubric(store: ContentStore, value: str) -> RubricEntry:
    """Resolve a rubric id, name or alias to a registry entry."""
    if not value or not value.strip():
        raise ValueError("rubric is empty")

    wanted = normalize(value)
    entries = list(store.rubrics())

    for entry in entries:
        if entry.rubric_id in (value, slugify(value)):
            return entry
    for entry in entries:
        if normalize(entry.name) == wanted or any(normalize(a) == wanted for a in entry.aliases):
            return entry

    close = difflib.get_close_matches(value, [e.name for e in entries], n=3, cutoff=0.5)
    raise UnknownRubric(
        f"{value}" + (f" (closest: {', '.join(close)})" if close else ""),
        [e.rubric_id for e in entries],
    )


def rubric_occurrences(
    store: ContentStore,
    rubric: str,
    date_from: str | None = None,
    date_to: str | None = None,
    *,
    disclaimer: str = "",
) -> RubricOccurrencesResult:
    """REQ-0017: every chapter carrying this rubric, most recent live first.

    Speakers are those *heard within* the chapter, not only those heading it: a
    rubric chapter has no heading speaker, so listing only the heads would
    return an empty list for exactly the chapters this tool is about.
    """
    entry = resolve_rubric(store, rubric)
    occurrences: list[RubricOccurrence] = []

    for live in store.lives(date_from, date_to):
        by_chapter_segments: dict[str, list] = {}
        for segment in live.segments:
            if segment.chapter_id is not None:
                by_chapter_segments.setdefault(segment.chapter_id, []).append(segment)

        mentions_by_segment: dict[str, set[str]] = {}
        for mention in live.mentions:
            mentions_by_segment.setdefault(mention.segment_id, set()).add(mention.instrument_id)

        for chapter in live.chapters:
            if chapter.rubric_id != entry.rubric_id:
                continue

            segments = by_chapter_segments.get(chapter.chapter_id, [])

            speaker_ids: list[str] = []
            for segment in segments:
                if segment.speaker.speaker_id not in speaker_ids:
                    speaker_ids.append(segment.speaker.speaker_id)

            instrument_ids: list[str] = []
            for segment in segments:
                for instrument_id in sorted(mentions_by_segment.get(segment.segment_id, ())):
                    if instrument_id not in instrument_ids:
                        instrument_ids.append(instrument_id)

            occurrences.append(
                RubricOccurrence(
                    live_id=live.live.live_id,
                    date=live.live.date,
                    chapter_id=chapter.chapter_id,
                    start_s=chapter.start_s,
                    timestamp_label=timestamp_label(chapter.start_s, chapter.start_quality),
                    start_quality=chapter.start_quality,
                    label_raw=chapter.label_raw,
                    topics_raw=chapter.topics_raw,
                    speaker_ids=speaker_ids,
                    speaker_names=[store.get_speaker(s).name for s in speaker_ids],
                    instrument_ids=instrument_ids,
                    instrument_names=[store.get_instrument(i).name for i in instrument_ids],
                    segment_count=len(segments),
                    caveats=live_caveats(live, segments),
                )
            )

    return RubricOccurrencesResult(
        rubric_id=entry.rubric_id,
        rubric_name=entry.name,
        date_from=date_from,
        date_to=date_to,
        occurrences=occurrences,
        disclaimer=disclaimer,
    )
