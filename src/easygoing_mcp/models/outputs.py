"""Output models for tools and resources (section 5).

Every item derived from a segment is a `SegmentRef`, and every response
covering a live carries `LiveCaveats` built from that live's data — never
hardcoded, because the two lives differ in exactly the ways a reader needs to
know about (section 4.6).

Returning Pydantic models is what makes the SDK emit `structuredContent` and an
`outputSchema`; the text rendering alongside it must say the same thing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .canonical import AnomalyKind, MentionIndexScope, StartQuality


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SegmentRef(_Output):
    """One segment, as every tool and resource returns it (section 5).

    `text` is the transcript verbatim. Numbers inside it are never extracted,
    normalized or corrected: both sources state that spoken figures are
    ASR-garbled and unverified (section 4.6).
    """

    live_id: str
    segment_id: str
    start_s: int
    end_s: int
    start_quality: StartQuality

    timestamp_label: str
    """`hh:mm:ss`, prefixed with `~` when `start_quality` is not exact."""

    speaker_id: str
    speaker_name: str
    attribution_confidence: str
    """The source's own vocabulary (`contextual`/`review_audio`, `alta`/`media`).
    Displayed, never compared across formats — the scales are unrelated."""

    needs_audio_review: bool
    """True where the speaker label is flagged for audio verification."""

    mixed_speakers: bool | None
    text: str

    deep_link: str | None
    """Null when the live has no video URL (MB20261029 has none)."""

    deep_link_approximate: bool = False
    """True when the link is built from an estimated timestamp."""


class AnomalyNote(_Output):
    """An anomaly, surfaced with the data it concerns rather than logged away."""

    kind: AnomalyKind
    detail: str
    segment_ids: list[str] = Field(default_factory=list)
    chapter_id: str | None = None


class LiveCaveats(_Output):
    """What a reader must know about the live behind a response (section 5).

    Mention counts are not comparable across these two lives, their timestamps
    are not equally trustworthy, and one of them stops mid-sentence. All of it
    is read off the live, so it stays true if the data changes.
    """

    live_id: str
    truncated: bool
    mention_index_scope: MentionIndexScope
    timestamps_estimated: bool
    speakers_are_inferred: bool = True
    """Both lives attribute speakers from context, not verified audio (section 4.6)."""

    anomalies: list[AnomalyNote] = Field(default_factory=list)
    """Live-level anomalies, plus those touching the returned segments."""

    notes: list[str] = Field(default_factory=list)
    """Plain-language statements of the above, for the text rendering."""


# --- REQ-0021: easygoing://lives ------------------------------------------

class LiveIndexEntry(_Output):
    live_id: str
    date: str
    title: str | None
    programme_id: str
    programme_name: str
    guest_speaker_names: list[str]
    """Speakers heading a guest block, in chapter order."""

    chapter_count: int
    segment_count: int
    truncated: bool
    has_video: bool
    """False means no deep links can be built for this live."""


class LivesIndex(_Output):
    lives: list[LiveIndexEntry]
    """Most recent first."""


# --- REQ-0002: easygoing://lives/{live_id}/chapters ------------------------

class ChapterView(_Output):
    chapter_id: str
    start_s: int
    timestamp_label: str
    start_quality: StartQuality
    label_raw: str
    rubric_id: str | None
    rubric_name: str | None
    speaker_ids: list[str]
    speaker_names: list[str]
    topics_raw: str | None
    """Free text as broadcast. Never parsed into topics (section 4.4)."""

    segment_count: int


class ChaptersView(_Output):
    live_id: str
    date: str
    chapters: list[ChapterView]
    caveats: LiveCaveats


# --- REQ-0003: easygoing://lives/{live_id}/instruments ---------------------

class InstrumentView(_Output):
    instrument_id: str
    name: str
    tickers: list[str]
    asset_class: str | None
    """Null until curated; never guessed (section 4.4)."""

    mention_count: int
    """Segments the source index links to this instrument. Airtime, not importance."""

    speaker_ids: list[str]
    speaker_names: list[str]
    first_mention: SegmentRef


class InstrumentsView(_Output):
    live_id: str
    date: str
    mention_index_scope: MentionIndexScope
    """`equities_only` vs `multi_asset`: counts are not comparable across lives."""

    instruments: list[InstrumentView]
    caveats: LiveCaveats


# --- REQ-0011: easygoing://speakers/{speaker_id} ---------------------------

class SpeakerLiveAppearance(_Output):
    live_id: str
    date: str
    title: str | None
    segment_count: int
    chapter_ids: list[str]
    """Chapters this speaker heads, if any."""


class SpeakerView(_Output):
    speaker_id: str
    name: str
    aliases: list[str]

    role: str | None
    affiliation: str | None
    links: list[str]
    disclosures: list[str]

    is_curated: bool
    """False means role, affiliation, links and disclosures are unfilled, not empty."""

    curation_note: str
    """Says so in words, so an empty field is never read as a finding."""

    appearances: list[SpeakerLiveAppearance]
    """Lives this speaker has segments in, most recent first."""


ResourceKind = Literal["lives", "chapters", "instruments", "speaker", "summary"]


# --- Tool outputs ----------------------------------------------------------

class SubjectResolution(_Output):
    """How a subject string was understood (section 7).

    `variants_used` lists forms searched that the user did not type, so a
    response is never silent about having broadened the question.
    """

    subject: str
    resolved_as: str
    match_kind: Literal["registry", "text"]
    instrument_ids: list[str] = Field(default_factory=list)
    terms_searched: list[str] = Field(default_factory=list)
    variants_used: list[str] = Field(default_factory=list)


class LiveMentionGroup(_Output):
    """Segments from one live, in broadcast order."""

    live_id: str
    date: str
    title: str | None
    segments: list[SegmentRef]
    caveats: LiveCaveats


class FindMentionsResult(_Output):
    """REQ-0005 / REQ-0006."""

    subject: SubjectResolution
    total_matches: int
    returned: int
    next_cursor: str | None
    """Opaque; pass back as `cursor` for the following page. Null when done."""

    groups: list[LiveMentionGroup]
    """Grouped by live, most recent live first."""

    comparable_across_lives: bool
    """False when the covered lives index mentions at different scopes."""

    notes: list[str] = Field(default_factory=list)


class SpeakerDiscussionGroup(_Output):
    speaker_id: str
    speaker_name: str
    attribution_confidence_values: list[str]
    any_needs_audio_review: bool
    segments: list[SegmentRef]
    """The matching segments plus adjacent context, in broadcast order."""

    matched_segment_ids: list[str]
    """Which of the above actually matched, as opposed to being context."""


class SubjectDiscussionResult(_Output):
    """REQ-0007. Advice-risk: carries a disclaimer."""

    live_id: str
    date: str
    subject: SubjectResolution
    groups: list[SpeakerDiscussionGroup]
    """One per speaker, speakers ordered by their first matching segment."""

    total_matches: int
    truncated_groups: list[str] = Field(default_factory=list)
    """Speakers whose group hit the per-group cap."""

    caveats: LiveCaveats
    disclaimer: str


class SpeakerCoverageResult(_Output):
    """REQ-0010."""

    live_id: str
    date: str
    speaker_id: str
    speaker_name: str
    segment_count: int
    instruments: list[InstrumentView]
    """Instruments mentioned in this speaker's own segments."""

    topics_raw: list[str]
    """`topics_raw` of chapters listing this speaker. Free text, not a taxonomy."""

    chapter_ids: list[str]
    mention_index_scope: MentionIndexScope
    caveats: LiveCaveats


class RankedSubject(_Output):
    instrument_id: str
    name: str
    tickers: list[str]
    mention_count: int
    """Segments mentioning it — airtime, not importance."""

    live_ids: list[str]


class RankSubjectsResult(_Output):
    """REQ-0015."""

    kind: Literal["instrument"]
    date_from: str | None
    date_to: str | None
    live_ids_covered: list[str]
    subjects: list[RankedSubject]
    comparable: bool
    """False when the covered lives index mentions at different scopes."""

    measure: str = "mention_count"
    warnings: list[str] = Field(default_factory=list)


class RubricOccurrence(_Output):
    live_id: str
    date: str
    chapter_id: str
    start_s: int
    timestamp_label: str
    start_quality: StartQuality
    label_raw: str
    topics_raw: str | None
    speaker_ids: list[str]
    speaker_names: list[str]
    """Speakers heard within the chapter, not only those heading it."""

    instrument_ids: list[str]
    instrument_names: list[str]
    segment_count: int
    caveats: LiveCaveats


class RubricOccurrencesResult(_Output):
    """REQ-0017. Advice-risk: carries a disclaimer."""

    rubric_id: str
    rubric_name: str
    date_from: str | None
    date_to: str | None
    occurrences: list[RubricOccurrence]
    """Most recent live first."""

    disclaimer: str
