"""Pydantic models of the RAW `mb_mymeeting_en_v0` format.

Source: Market_Briefing_2026-10-28_MCP_TEST.json.

These mirror the source exactly — same key names, same shapes, no
interpretation. `extra="forbid"` is deliberate: a field we have never seen must
stop the ingest rather than be dropped silently (section 2, "no best-effort
parsing"; section 4.1, "nothing is dropped silently").
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EnV0Chapter(_Strict):
    youtube: str
    """Official chapter start, `mm:ss` or `h:mm:ss`."""

    label: str
    """Either `Left — Right` or a bare name; classified during adaptation."""


class EnV0Security(_Strict):
    name: str
    ticker: str
    first_mention: str
    """Timecode of the first mention. Recomputed and cross-checked, not stored."""

    speakers: list[str]
    """Recomputed and cross-checked, not stored."""

    segments: list[str]
    """Canonical-shaped segment ids, e.g. `MB20261028-001`."""


class EnV0Segment(_Strict):
    id: str
    timestamp_youtube: str
    timestamp_original: str
    """Recording-clock time; a constant 225 s ahead of the broadcast clock."""

    timecode_kind: str
    speaker: str
    text: str
    confidence: str
    source_line: int
    speaker_correction: str | None
    """Set on exactly the segments flagged `review_audio`."""


class EnV0SearchChunk(_Strict):
    """A derived retrieval chunking. Ignored by ingest (section 4.2)."""

    chunk_id: str
    parent_segment_id: str
    speaker: str
    timestamp_youtube_source: str
    timecode_precision: str
    text: str
    securities: list[str]


class EnV0Metadata(_Strict):
    source: str
    """Free text carrying the broadcast date, e.g. `... , 28 Oct 2026`."""

    video: str
    transcription_source_duration: str
    on_air_start_original: str
    off_air_start_original: str
    recording_prefix_removed: str
    broadcast_content_until_youtube_approx: str
    timecode_precision: str
    disclaimer: str
    chapters: list[EnV0Chapter]
    security: str
    search_chunks_count: int
    search_chunks_note: str


class EnV0Document(_Strict):
    metadata: EnV0Metadata
    securities_index: list[EnV0Security]
    segments: list[EnV0Segment] = Field(min_length=1)
    search_chunks: list[EnV0SearchChunk]
