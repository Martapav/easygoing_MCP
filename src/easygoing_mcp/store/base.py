"""The `ContentStore` protocol: the one seam between content and MCP.

Everything in `services/` depends on this protocol and nothing else, so the
JSON prototype can be replaced by PostgreSQL inside the EasyGoing app without
touching the MCP surface (section 1).

Two rules keep that swap honest:

* Queries are *pushed down*, not filtered afterwards. `search_segments` takes a
  whole `SegmentQuery` — filters, ordering and a window — so a SQL backend can
  answer it with one statement and full-text search (Italian configuration) or
  pgvector, exactly where the JSON store does a linear scan over precomputed
  normalized text (section 7).
* Ordering is part of the contract, not an accident of storage: lives newest
  first, segments by live date descending then `start_s` ascending, ties broken
  by `segment_id`. Section 7 specifies that ordering for `find_mentions`, and a
  window over an undefined order would page incoherently.

Lookup failures raise, with the valid options or closest matches in the
message, so a model that guessed an id can correct itself (section 5).
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..models.canonical import CanonicalLive, Segment
from ..models.registries import InstrumentEntry, ProgrammeEntry, RubricEntry, SpeakerEntry


class StoreError(Exception):
    """Base class for store lookup failures."""


class UnknownId(StoreError):
    """An id that does not exist, reported with the alternatives.

    FastMCP/MCPServer turns a tool exception into an `isError` result, so this
    message is what the model sees and retries against.
    """

    def __init__(self, kind: str, requested: str, known: Sequence[str]) -> None:
        self.kind = kind
        self.requested = requested
        self.known = list(known)

        close = difflib.get_close_matches(requested, self.known, n=3, cutoff=0.6)
        if close:
            hint = f"Did you mean: {', '.join(close)}?"
        elif len(self.known) <= 20:
            hint = f"Valid values: {', '.join(sorted(self.known))}." if self.known else "None exist."
        else:
            hint = f"{len(self.known)} valid values exist; none resemble it."
        super().__init__(f"unknown {kind} {requested!r}. {hint}")


class UnknownLive(UnknownId):
    def __init__(self, requested: str, known: Sequence[str]) -> None:
        super().__init__("live_id", requested, known)


class UnknownSpeaker(UnknownId):
    def __init__(self, requested: str, known: Sequence[str]) -> None:
        super().__init__("speaker_id", requested, known)


class UnknownRubric(UnknownId):
    def __init__(self, requested: str, known: Sequence[str]) -> None:
        super().__init__("rubric", requested, known)


class UnknownInstrument(UnknownId):
    def __init__(self, requested: str, known: Sequence[str]) -> None:
        super().__init__("instrument", requested, known)


class UnknownProgramme(UnknownId):
    def __init__(self, requested: str, known: Sequence[str]) -> None:
        super().__init__("programme", requested, known)


class UnknownSegment(UnknownId):
    def __init__(self, requested: str, known: Sequence[str]) -> None:
        super().__init__("segment_id", requested, known)


@dataclass(frozen=True)
class SegmentQuery:
    """A segment search, expressed so a backend can answer it in one pass.

    Populated filters combine with AND, and several values within one filter
    mean OR — with one deliberate exception: `instrument_ids` and `text_any` are
    the two ways of locating the *same* subject, so they combine with each other
    as OR. Section 7 resolves a registry subject using "`mentions[]` plus a text
    match on the segment for the name and aliases", which is that union; AND-ing
    them would drop every segment the index missed, and the index is exactly what
    we do not trust to be complete.

    `text_any` holds already-normalized needles (see `ingest.slugs.normalize`).
    Normalization is the caller's job because the subject resolver decides what
    counts as a term, while the store only decides how to match it.
    """

    live_ids: Sequence[str] | None = None
    speaker_ids: Sequence[str] | None = None
    chapter_ids: Sequence[str] | None = None
    segment_ids: Sequence[str] | None = None

    # Inclusive ISO date bounds on the live, e.g. "2026-10-28".
    date_from: str | None = None
    date_to: str | None = None

    # Segments carrying a mention of any of these instruments (from mentions[]).
    instrument_ids: Sequence[str] | None = None

    # Normalized needles matched against normalized segment text.
    text_any: Sequence[str] | None = None

    # A window over the contract ordering. `limit=None` means no window, which
    # only internal callers may use — every MCP-facing list is bounded (section 5).
    limit: int | None = None
    offset: int = 0

    def without_window(self) -> SegmentQuery:
        """The same query covering every match, for counting."""
        return SegmentQuery(
            live_ids=self.live_ids, speaker_ids=self.speaker_ids, chapter_ids=self.chapter_ids,
            segment_ids=self.segment_ids, date_from=self.date_from, date_to=self.date_to,
            instrument_ids=self.instrument_ids, text_any=self.text_any, limit=None, offset=0,
        )


@dataclass(frozen=True)
class SegmentHit:
    """One matching segment, with the live it belongs to.

    `matched_instrument_ids` says which of the query's instruments the segment
    was indexed against, so a caller can tell a registry-backed hit from a
    text-only one without re-deriving it (section 7, `match_kind`).
    """

    live_id: str
    segment: Segment
    matched_instrument_ids: tuple[str, ...] = ()
    matched_text: bool = False


@dataclass(frozen=True)
class SegmentPage:
    """A window of hits plus what is needed to page on.

    `total` counts every match, not just this window, so a caller can say
    whether more exist without asking twice.
    """

    items: Sequence[SegmentHit] = field(default_factory=tuple)
    total: int = 0
    offset: int = 0

    @property
    def next_offset(self) -> int | None:
        following = self.offset + len(self.items)
        return following if following < self.total else None


@runtime_checkable
class ContentStore(Protocol):
    """Read-only access to canonical transcripts and registries.

    Every method is read-only: nothing in the MCP surface mutates content
    (section 7, "all tools read-only").
    """

    # --- lives -----------------------------------------------------------
    def live_ids(self) -> Sequence[str]:
        """Known live ids, newest live first."""
        ...

    def get_live(self, live_id: str) -> CanonicalLive:
        """One live. Raises `UnknownLive`.

        Expected to be cheap and repeatable: callers fetch a live per result
        group rather than threading it through every return value.
        """
        ...

    def lives(self, date_from: str | None = None, date_to: str | None = None) -> Sequence[CanonicalLive]:
        """Lives within an inclusive ISO date range, newest first."""
        ...

    # --- segments --------------------------------------------------------
    def get_segment(self, live_id: str, segment_id: str) -> Segment:
        """One segment. Raises `UnknownLive` or `UnknownSegment`."""
        ...

    def search_segments(self, query: SegmentQuery) -> SegmentPage:
        """Segments matching `query`, in the contract ordering.

        This is the method a PostgreSQL backend reimplements with full-text
        search; no caller may filter segments itself (section 7).
        """
        ...

    def adjacent_segments(self, live_id: str, segment_id: str, before: int, after: int) -> Sequence[Segment]:
        """Neighbouring segments in broadcast order, for contiguous context.

        Used by `get_subject_discussion`, which needs the matching segment plus
        adjacent segments of the same speaker in the same chapter (section 7).
        Filtering by speaker and chapter is the caller's; this only supplies the
        window, because "adjacent" is an ordering property the store owns.
        """
        ...

    # --- aggregates ------------------------------------------------------
    def count_mentions(
        self, date_from: str | None = None, date_to: str | None = None
    ) -> Mapping[str, int]:
        """Mention count per instrument id over a date range.

        An aggregate a SQL backend should answer with GROUP BY rather than by
        shipping every mention to the caller (section 7, `rank_subjects`).
        """
        ...

    def live_ids_for_speaker(self, speaker_id: str) -> Sequence[str]:
        """Lives in which a speaker has at least one segment, newest first.

        Raises `UnknownSpeaker` for an id absent from the registry.
        """
        ...

    # --- registries ------------------------------------------------------
    def get_speaker(self, speaker_id: str) -> SpeakerEntry: ...
    def get_rubric(self, rubric_id: str) -> RubricEntry: ...
    def get_instrument(self, instrument_id: str) -> InstrumentEntry: ...
    def get_programme(self, programme_id: str) -> ProgrammeEntry: ...

    def speakers(self) -> Sequence[SpeakerEntry]: ...
    def rubrics(self) -> Sequence[RubricEntry]: ...
    def instruments(self) -> Sequence[InstrumentEntry]: ...
    def programmes(self) -> Sequence[ProgrammeEntry]: ...
