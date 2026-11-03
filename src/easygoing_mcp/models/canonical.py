"""Pydantic models of the canonical format (section 4.1).

This is the only transcript shape the MCP layer ever sees. Both source formats
converge here, so nothing downstream can tell en_v0 from it_v1 except by
reading `live.source_format` and the coverage flags.

Every enum is a closed `Literal`: an unrecognised value must fail validation
rather than flow through as a string nobody handles (section 9).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SourceFormat = Literal["mb_mymeeting_en_v0", "mb_mymeeting_it_v1", "transcript_txt_v1"]

StartQuality = Literal[
    "asr_chunk_start",            # raw ASR chunk boundary (en_v0)
    "chapter_official",           # published chapter time
    "chapter_official_unverified",  # published chapter time carrying an annotation
    "estimated_interpolated",     # interpolated between anchors (it_v1)
]

MentionIndexScope = Literal["equities_only", "multi_asset"]

TimestampsKind = Literal["asr_chunk_start", "estimated_interpolated"]

MentionOrigin = Literal[
    "source_index",      # the source index lists the segment explicitly (JSON sources)
    "index_text_match",  # the source index names the instrument; segments found by text match (.txt)
]

AnomalyKind = Literal[
    "zero_duration",               # end_s == start_s, inherent to the timecode model
    "chapter_assignment_conflict",  # rubrica-based and time-based assignment disagree
    "source_limitation",           # a limitation the source itself declares
]

# start_quality values that are exact enough to show without a "~" prefix (section 5).
EXACT_START_QUALITIES: frozenset[str] = frozenset({"chapter_official"})


class _Canonical(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SpeakerAttribution(_Canonical):
    """Who is speaking, and how much that attribution can be trusted.

    `confidence` keeps each source's own vocabulary (`contextual`/`review_audio`
    for en_v0, `alta`/`media` for it_v1) and is for display only — mapping two
    unrelated scales onto one would invent precision. Logic uses
    `needs_audio_review` (section 4.1).
    """

    speaker_id: str
    confidence: str
    needs_audio_review: bool


class Coverage(_Canonical):
    """What part of the broadcast this file actually covers, and how well."""

    content_end_s: int = Field(ge=0)
    truncated: bool
    mention_index_scope: MentionIndexScope
    timestamps_kind: TimestampsKind


class Live(_Canonical):
    live_id: str
    date: str
    """ISO `YYYY-MM-DD`."""

    title: str | None
    programme_id: str
    video_url: str | None
    """Absent for MB20261029, which is why deep links are nullable everywhere."""

    source_format: SourceFormat
    source_file: str
    duration_seconds: int | None
    ingest_schema_version: str
    source_meta: dict[str, Any] = Field(default_factory=dict)
    """Live-level source fields with no canonical home. Nothing is dropped."""


class Chapter(_Canonical):
    chapter_id: str
    """`<live_id>-C<NN>`."""

    start_s: int = Field(ge=0)
    start_quality: StartQuality
    label_raw: str
    """The source label verbatim, before guest/rubric classification."""

    rubric_id: str | None
    speaker_ids: list[str]
    topics_raw: str | None
    """Free text. Never parsed into a topic registry (section 4.4)."""

    @model_validator(mode="after")
    def _guest_or_rubric(self) -> Chapter:
        # A chapter is a guest block (speakers, no rubric) or a rubric block.
        # Both at once would make rubric occurrence counting ambiguous.
        if self.rubric_id is not None and self.speaker_ids:
            raise ValueError(
                f"chapter {self.chapter_id} has both rubric_id={self.rubric_id!r} "
                f"and speaker_ids={self.speaker_ids!r}; it must be one or the other"
            )
        return self


class Segment(_Canonical):
    segment_id: str
    start_s: int = Field(ge=0)
    end_s: int = Field(ge=0)
    start_quality: StartQuality
    chapter_id: str | None
    text: str
    speaker: SpeakerAttribution
    mixed_speakers: bool | None
    """Null unless the source marks it per segment. Neither source does, so it is
    null everywhere; it is never inferred from the text (section 4.1)."""

    source: dict[str, Any] = Field(default_factory=dict)
    """Original per-segment fields, untouched."""

    @model_validator(mode="after")
    def _end_not_before_start(self) -> Segment:
        if self.end_s < self.start_s:
            raise ValueError(
                f"segment {self.segment_id}: end_s={self.end_s} precedes start_s={self.start_s}"
            )
        return self


class Mention(_Canonical):
    """One instrument occurring in one segment.

    `origin` says how the pairing is known: listed by the source index, or found
    by matching an index entry's names and tickers in the segment text.
    """

    instrument_id: str
    segment_id: str
    origin: MentionOrigin


class Anomaly(_Canonical):
    """Something known to be off about the data, carried with the data.

    Surfaced to clients through LiveCaveats (section 5) rather than hidden in a
    log, because a model reading the transcript needs to know.
    """

    kind: AnomalyKind
    segment_ids: list[str] = Field(default_factory=list)
    chapter_id: str | None = None
    detail: str


class CanonicalLive(_Canonical):
    """One canonical file: `data/canonical/<live_id>.canonical.json`."""

    live: Live
    coverage: Coverage
    chapters: list[Chapter]
    segments: Annotated[list[Segment], Field(min_length=1)]
    mentions: list[Mention]
    anomalies: list[Anomaly]

    @model_validator(mode="after")
    def _internally_consistent(self) -> CanonicalLive:
        chapter_ids = {c.chapter_id for c in self.chapters}
        segment_ids = {s.segment_id for s in self.segments}

        if len(chapter_ids) != len(self.chapters):
            raise ValueError(f"{self.live.live_id}: duplicate chapter_id")
        if len(segment_ids) != len(self.segments):
            raise ValueError(f"{self.live.live_id}: duplicate segment_id")

        for segment in self.segments:
            if segment.chapter_id is not None and segment.chapter_id not in chapter_ids:
                raise ValueError(
                    f"segment {segment.segment_id} references unknown chapter {segment.chapter_id}"
                )
        for mention in self.mentions:
            if mention.segment_id not in segment_ids:
                raise ValueError(
                    f"mention {mention.instrument_id} references unknown segment {mention.segment_id}"
                )

        starts = [s.start_s for s in self.segments]
        if starts != sorted(starts):
            raise ValueError(f"{self.live.live_id}: segments are not ordered by start_s")

        return self
