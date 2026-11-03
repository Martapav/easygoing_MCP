"""Instrument mention projections (REQ-0003)."""

from __future__ import annotations

from collections import defaultdict

from ..models.outputs import (
    InstrumentsView,
    InstrumentView,
    RankedSubject,
    RankSubjectsResult,
)
from ..store.base import ContentStore
from .lives import live_caveats, segment_ref


def instruments_view(store: ContentStore, live_id: str) -> InstrumentsView:
    """REQ-0003: instruments the source index links to this live.

    `mention_count` is how many segments the index attaches to an instrument —
    airtime, not importance — and it is not comparable with the other live,
    whose index covers a different scope. Both facts are carried in the
    response rather than left for the reader to know (sections 4.6 and 5).
    """
    live = store.get_live(live_id)
    by_segment = {segment.segment_id: segment for segment in live.segments}

    segments_by_instrument: dict[str, list[str]] = defaultdict(list)
    for mention in live.mentions:
        segments_by_instrument[mention.instrument_id].append(mention.segment_id)

    views: list[InstrumentView] = []
    for instrument_id, segment_ids in segments_by_instrument.items():
        entry = store.get_instrument(instrument_id)
        segments = sorted(
            (by_segment[sid] for sid in segment_ids), key=lambda s: (s.start_s, s.segment_id)
        )

        speaker_ids: list[str] = []
        for segment in segments:
            if segment.speaker.speaker_id not in speaker_ids:
                speaker_ids.append(segment.speaker.speaker_id)

        views.append(
            InstrumentView(
                instrument_id=instrument_id,
                name=entry.name,
                tickers=list(entry.tickers),
                asset_class=entry.asset_class,
                mention_count=len(segment_ids),
                speaker_ids=speaker_ids,
                speaker_names=[store.get_speaker(sid).name for sid in speaker_ids],
                first_mention=segment_ref(store, live, segments[0]),
            )
        )

    # Most-mentioned first, then by name for a stable order.
    views.sort(key=lambda v: (-v.mention_count, v.name))

    return InstrumentsView(
        live_id=live.live.live_id,
        date=live.live.date,
        mention_index_scope=live.coverage.mention_index_scope,
        instruments=views,
        caveats=live_caveats(live, live.segments),
    )


class TopicRankingUnavailable(ValueError):
    """`kind="topic"` has no data behind it (sections 4.4 and 7).

    There is deliberately no topics registry, so topics cannot be counted;
    ranking them would mean inventing the categories being ranked.
    """


def rank_subjects(
    store: ContentStore,
    date_from: str | None = None,
    date_to: str | None = None,
    kind: str = "instrument",
    limit: int = 10,
) -> RankSubjectsResult:
    """REQ-0015: mention counts per subject over a date range.

    The measure is `mention_count` — how many segments the source index links to
    a subject, which is airtime rather than importance. When the covered lives
    index mentions at different scopes the result is flagged `comparable: false`,
    because MB20261028 indexes single equities only while MB20261029 is
    multi-asset: ranking them together would put a commodity that could only be
    counted in one live below an equity counted in both.
    """
    if kind != "instrument":
        raise TopicRankingUnavailable(
            f"kind={kind!r} is not available. Only 'instrument' can be ranked in this phase: "
            "there is no topics registry, so topics have no countable identity. Chapter topic "
            "text is available verbatim through the chapters resource."
        )

    lives = list(store.lives(date_from, date_to))
    scopes = {live.coverage.mention_index_scope for live in lives}
    comparable = len(scopes) <= 1

    live_ids_by_instrument: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    for live in lives:
        for mention in live.mentions:
            counts[mention.instrument_id] = counts.get(mention.instrument_id, 0) + 1
            ids = live_ids_by_instrument.setdefault(mention.instrument_id, [])
            if live.live.live_id not in ids:
                ids.append(live.live.live_id)

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[: max(0, limit)]
    subjects = []
    for instrument_id, count in ranked:
        entry = store.get_instrument(instrument_id)
        subjects.append(
            RankedSubject(
                instrument_id=instrument_id,
                name=entry.name,
                tickers=list(entry.tickers),
                mention_count=count,
                live_ids=live_ids_by_instrument[instrument_id],
            )
        )

    warnings: list[str] = [
        "mention_count measures how many segments mention a subject: airtime, not importance."
    ]
    if not comparable:
        warnings.append(
            "The lives in this range index mentions at different scopes ("
            + ", ".join(sorted(scopes))
            + "), so these counts are NOT comparable across lives: a subject outside a live's "
            "index scope cannot be counted in that live at all."
        )

    return RankSubjectsResult(
        kind="instrument",
        date_from=date_from,
        date_to=date_to,
        live_ids_covered=[live.live.live_id for live in lives],
        subjects=subjects,
        comparable=comparable,
        warnings=warnings,
    )
