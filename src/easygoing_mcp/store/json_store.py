"""`ContentStore` backed by the JSON files under `data/` (section 2).

Everything is loaded and indexed once at construction: with two lives and 378
segments a linear scan is correct and fast, and section 7 explicitly rules out
embeddings or a vector DB in Phase 1. The indexes exist so the access patterns
match what a SQL backend would do, not because the volume demands them.

Segment text is normalized once at load and matched with token boundaries, so
`Oro` does not match `lavoro` — Italian makes naive substring search wrong
often enough to matter.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence

from ..config import Settings
from ..ingest.slugs import normalize
from ..models.canonical import CanonicalLive, Segment
from ..models.registries import InstrumentEntry, ProgrammeEntry, RubricEntry, SpeakerEntry
from .base import (
    SegmentHit,
    SegmentPage,
    SegmentQuery,
    StoreError,
    UnknownInstrument,
    UnknownLive,
    UnknownProgramme,
    UnknownRubric,
    UnknownSegment,
    UnknownSpeaker,
)

# stdio transport: the server owns stdout, so logs go to stderr (section 3).
logger = logging.getLogger(__name__)


def _term_pattern(term: str) -> re.Pattern[str]:
    """Match `term` on token boundaries within already-normalized text.

    `\\b` is not usable here: normalized terms may contain `'`, `&` or `/`, where
    Python's word boundary falls inside the term rather than around it.
    """
    return re.compile(rf"(?<![0-9a-z]){re.escape(term)}(?![0-9a-z])")


class JsonContentStore:
    """Loads `data/canonical/` and `data/registries/` at startup."""

    def __init__(self, settings: Settings, *, strict: bool = True) -> None:
        self._settings = settings
        self._lives: dict[str, CanonicalLive] = {}
        self._speakers: dict[str, SpeakerEntry] = {}
        self._rubrics: dict[str, RubricEntry] = {}
        self._instruments: dict[str, InstrumentEntry] = {}
        self._programmes: dict[str, ProgrammeEntry] = {}

        # Derived indexes, all rebuilt from the files above.
        self._live_order: list[str] = []
        self._live_rank: dict[str, int] = {}
        self._segments: dict[str, dict[str, Segment]] = {}
        self._segment_order: dict[str, list[str]] = {}
        self._normalized_text: dict[str, str] = {}
        self._instruments_by_segment: dict[str, frozenset[str]] = {}
        self._lives_by_speaker: dict[str, list[str]] = defaultdict(list)
        self._term_cache: dict[str, re.Pattern[str]] = {}

        self._load(strict=strict)

    # -- loading ----------------------------------------------------------

    def _load(self, *, strict: bool) -> None:
        self._load_registries()
        self._load_lives()
        self._build_indexes()
        if strict:
            self._check_registry_coverage()
        logger.info(
            "loaded %d lives, %d segments, %d instruments from %s",
            len(self._lives),
            sum(len(s) for s in self._segments.values()),
            len(self._instruments),
            self._settings.data_dir,
        )

    def _read_registry(self, name: str, model, key: str) -> dict:
        path = self._settings.registries_dir / name
        if not path.exists():
            raise StoreError(
                f"registry {path} is missing. Run scripts/ingest.py to seed the registries."
            )
        entries = [model.model_validate(row) for row in json.loads(path.read_text(encoding="utf-8"))]
        return {getattr(entry, key): entry for entry in entries}

    def _load_registries(self) -> None:
        self._speakers = self._read_registry("speakers.json", SpeakerEntry, "speaker_id")
        self._rubrics = self._read_registry("rubrics.json", RubricEntry, "rubric_id")
        self._instruments = self._read_registry("instruments.json", InstrumentEntry, "instrument_id")
        self._programmes = self._read_registry("programmes.json", ProgrammeEntry, "programme_id")

    def _load_lives(self) -> None:
        directory = self._settings.canonical_dir
        paths = sorted(directory.glob("*.canonical.json"))
        if not paths:
            raise StoreError(
                f"no canonical files in {directory}. Run scripts/ingest.py to build them "
                "from data/source/."
            )
        for path in paths:
            live = CanonicalLive.model_validate_json(path.read_text(encoding="utf-8"))
            if live.live.live_id in self._lives:
                raise StoreError(f"duplicate live_id {live.live.live_id} in {path}")
            self._lives[live.live.live_id] = live

    def _build_indexes(self) -> None:
        # Contract ordering: newest live first, ties broken by id for determinism.
        self._live_order = sorted(
            self._lives, key=lambda lid: (self._lives[lid].live.date, lid), reverse=True
        )
        self._live_rank = {live_id: rank for rank, live_id in enumerate(self._live_order)}

        for live_id, live in self._lives.items():
            self._segments[live_id] = {s.segment_id: s for s in live.segments}
            # Canonical files are validated as ordered by start_s, so document
            # order is broadcast order.
            self._segment_order[live_id] = [s.segment_id for s in live.segments]

            for segment in live.segments:
                self._normalized_text[segment.segment_id] = normalize(segment.text)

            by_segment: dict[str, set[str]] = defaultdict(set)
            for mention in live.mentions:
                by_segment[mention.segment_id].add(mention.instrument_id)
            self._instruments_by_segment.update(
                {sid: frozenset(ids) for sid, ids in by_segment.items()}
            )

            speakers_here = {s.speaker.speaker_id for s in live.segments}
            for speaker_id in speakers_here:
                self._lives_by_speaker[speaker_id].append(live_id)

        for live_ids in self._lives_by_speaker.values():
            live_ids.sort(key=lambda lid: self._live_rank[lid])

    def _check_registry_coverage(self) -> None:
        """Fail at startup rather than mid-request on an unregistered id.

        `scripts/validate_data.py` reports the same thing; this repeats it so a
        server never serves a live whose ids it cannot resolve.
        """
        problems: list[str] = []
        for live_id, live in self._lives.items():
            used_speakers = {s.speaker.speaker_id for s in live.segments}
            used_speakers |= {sid for c in live.chapters for sid in c.speaker_ids}
            if missing := sorted(used_speakers - self._speakers.keys()):
                problems.append(f"{live_id}: speakers not in speakers.json: {missing}")
            if missing := sorted(
                {c.rubric_id for c in live.chapters if c.rubric_id} - self._rubrics.keys()
            ):
                problems.append(f"{live_id}: rubrics not in rubrics.json: {missing}")
            if missing := sorted({m.instrument_id for m in live.mentions} - self._instruments.keys()):
                problems.append(f"{live_id}: instruments not in instruments.json: {missing}")
            if live.live.programme_id not in self._programmes:
                problems.append(f"{live_id}: programme {live.live.programme_id!r} not in programmes.json")

        if problems:
            raise StoreError(
                "canonical files reference ids missing from the registries. "
                "Re-run scripts/ingest.py. Details: " + "; ".join(problems)
            )

    # -- lives --------------------------------------------------------------

    def live_ids(self) -> Sequence[str]:
        return tuple(self._live_order)

    def get_live(self, live_id: str) -> CanonicalLive:
        try:
            return self._lives[live_id]
        except KeyError:
            raise UnknownLive(live_id, self._live_order) from None

    def lives(self, date_from: str | None = None, date_to: str | None = None) -> Sequence[CanonicalLive]:
        return tuple(
            self._lives[live_id]
            for live_id in self._live_order
            if self._in_range(self._lives[live_id].live.date, date_from, date_to)
        )

    @staticmethod
    def _in_range(date: str, date_from: str | None, date_to: str | None) -> bool:
        # ISO dates compare correctly as strings.
        if date_from is not None and date < date_from:
            return False
        if date_to is not None and date > date_to:
            return False
        return True

    # -- segments -----------------------------------------------------------

    def get_segment(self, live_id: str, segment_id: str) -> Segment:
        self.get_live(live_id)  # raises UnknownLive
        try:
            return self._segments[live_id][segment_id]
        except KeyError:
            raise UnknownSegment(segment_id, self._segment_order[live_id]) from None

    def adjacent_segments(
        self, live_id: str, segment_id: str, before: int, after: int
    ) -> Sequence[Segment]:
        self.get_segment(live_id, segment_id)  # validates both ids
        order = self._segment_order[live_id]
        index = order.index(segment_id)
        start = max(0, index - max(0, before))
        stop = min(len(order), index + max(0, after) + 1)
        return tuple(self._segments[live_id][sid] for sid in order[start:stop])

    def _pattern(self, term: str) -> re.Pattern[str]:
        if term not in self._term_cache:
            self._term_cache[term] = _term_pattern(term)
        return self._term_cache[term]

    def search_segments(self, query: SegmentQuery) -> SegmentPage:
        live_ids = (
            [lid for lid in self._live_order if lid in set(query.live_ids)]
            if query.live_ids is not None
            else list(self._live_order)
        )
        speaker_filter = set(query.speaker_ids) if query.speaker_ids is not None else None
        chapter_filter = set(query.chapter_ids) if query.chapter_ids is not None else None
        segment_filter = set(query.segment_ids) if query.segment_ids is not None else None
        instrument_filter = set(query.instrument_ids) if query.instrument_ids else None
        terms = [t for t in (query.text_any or []) if t]

        hits: list[SegmentHit] = []
        for live_id in live_ids:
            live = self._lives[live_id]
            if not self._in_range(live.live.date, query.date_from, query.date_to):
                continue

            for segment_id in self._segment_order[live_id]:
                segment = self._segments[live_id][segment_id]

                if segment_filter is not None and segment_id not in segment_filter:
                    continue
                if speaker_filter is not None and segment.speaker.speaker_id not in speaker_filter:
                    continue
                if chapter_filter is not None and segment.chapter_id not in chapter_filter:
                    continue

                matched_instruments: tuple[str, ...] = ()
                matched_text = False

                if instrument_filter is not None:
                    overlap = self._instruments_by_segment.get(segment_id, frozenset()) & instrument_filter
                    matched_instruments = tuple(sorted(overlap))
                if terms:
                    text = self._normalized_text[segment_id]
                    matched_text = any(self._pattern(term).search(text) for term in terms)

                # instrument_ids and text_any locate the same subject, so they
                # are a union; with neither set, the other filters stand alone.
                if instrument_filter is not None or terms:
                    if not matched_instruments and not matched_text:
                        continue

                hits.append(
                    SegmentHit(
                        live_id=live_id,
                        segment=segment,
                        matched_instrument_ids=matched_instruments,
                        matched_text=matched_text,
                    )
                )

        total = len(hits)
        offset = max(0, query.offset)
        window = hits[offset:] if query.limit is None else hits[offset : offset + query.limit]
        return SegmentPage(items=tuple(window), total=total, offset=offset)

    # -- aggregates ---------------------------------------------------------

    def count_mentions(
        self, date_from: str | None = None, date_to: str | None = None
    ) -> Mapping[str, int]:
        counts: Counter[str] = Counter()
        for live in self.lives(date_from, date_to):
            counts.update(mention.instrument_id for mention in live.mentions)
        return dict(counts)

    def live_ids_for_speaker(self, speaker_id: str) -> Sequence[str]:
        if speaker_id not in self._speakers:
            raise UnknownSpeaker(speaker_id, sorted(self._speakers))
        return tuple(self._lives_by_speaker.get(speaker_id, ()))

    # -- registries ---------------------------------------------------------

    def get_speaker(self, speaker_id: str) -> SpeakerEntry:
        try:
            return self._speakers[speaker_id]
        except KeyError:
            raise UnknownSpeaker(speaker_id, sorted(self._speakers)) from None

    def get_rubric(self, rubric_id: str) -> RubricEntry:
        try:
            return self._rubrics[rubric_id]
        except KeyError:
            raise UnknownRubric(rubric_id, sorted(self._rubrics)) from None

    def get_instrument(self, instrument_id: str) -> InstrumentEntry:
        try:
            return self._instruments[instrument_id]
        except KeyError:
            raise UnknownInstrument(instrument_id, sorted(self._instruments)) from None

    def get_programme(self, programme_id: str) -> ProgrammeEntry:
        try:
            return self._programmes[programme_id]
        except KeyError:
            raise UnknownProgramme(programme_id, sorted(self._programmes)) from None

    def speakers(self) -> Sequence[SpeakerEntry]:
        return tuple(self._speakers[k] for k in sorted(self._speakers))

    def rubrics(self) -> Sequence[RubricEntry]:
        return tuple(self._rubrics[k] for k in sorted(self._rubrics))

    def instruments(self) -> Sequence[InstrumentEntry]:
        return tuple(self._instruments[k] for k in sorted(self._instruments))

    def programmes(self) -> Sequence[ProgrammeEntry]:
        return tuple(self._programmes[k] for k in sorted(self._programmes))


def load_store(settings: Settings | None = None) -> JsonContentStore:
    """Build the default store from settings (env-configured data dir)."""
    from ..config import load_settings

    return JsonContentStore(settings or load_settings())
