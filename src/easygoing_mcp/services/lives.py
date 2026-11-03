"""Live-level projections, and the builders every other service reuses.

`segment_ref` and `live_caveats` implement the output contract of section 5, so
they live here rather than being re-derived per tool: a caveat that differed
between two responses about the same live would be worse than none.

Nothing in `services/` imports MCP. These are plain functions over a
`ContentStore` (section 2).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from ..config import Settings
from ..ingest.timecodes import format_timecode
from ..models.canonical import EXACT_START_QUALITIES, CanonicalLive, Segment
from ..models.outputs import (
    AnomalyNote,
    LiveCaveats,
    LiveIndexEntry,
    LivesIndex,
    SegmentRef,
)
from ..store.base import ContentStore


class SummaryNotGenerated(FileNotFoundError):
    """No summary file exists for this live.

    Deliberately an error rather than an empty document: the server must never
    generate a summary itself (section 7, REQ-0001).
    """


def timestamp_label(start_s: int, start_quality: str) -> str:
    """`hh:mm:ss`, prefixed with `~` when the timecode is not exact (section 5)."""
    label = format_timecode(start_s)
    return label if start_quality in EXACT_START_QUALITIES else f"~{label}"


def deep_link(live: CanonicalLive, start_s: int) -> tuple[str | None, bool]:
    """`(url, approximate)` for a point in the broadcast.

    Built only when the live has a video URL — MB20261029 has none, so no deep
    link can exist for it and the field stays null rather than guessing one
    (section 5).
    """
    if not live.live.video_url:
        return None, False
    approximate = live.coverage.timestamps_kind != "asr_chunk_start"
    return f"{live.live.video_url}?t={int(start_s)}", approximate


def segment_ref(store: ContentStore, live: CanonicalLive, segment: Segment) -> SegmentRef:
    """Project a canonical segment into the shared output shape (section 5)."""
    url, approximate = deep_link(live, segment.start_s)
    return SegmentRef(
        live_id=live.live.live_id,
        segment_id=segment.segment_id,
        start_s=segment.start_s,
        end_s=segment.end_s,
        start_quality=segment.start_quality,
        timestamp_label=timestamp_label(segment.start_s, segment.start_quality),
        speaker_id=segment.speaker.speaker_id,
        speaker_name=store.get_speaker(segment.speaker.speaker_id).name,
        attribution_confidence=segment.speaker.confidence,
        needs_audio_review=segment.speaker.needs_audio_review,
        mixed_speakers=segment.mixed_speakers,
        text=segment.text,
        deep_link=url,
        deep_link_approximate=approximate,
    )


def live_caveats(live: CanonicalLive, segments: Iterable[Segment] = ()) -> LiveCaveats:
    """Caveats for a response about `live`, covering `segments` (section 5).

    Anomalies included are the live-level ones — which apply to any response
    about this live — plus those touching the returned segments. Anomalies about
    parts of the live nobody asked for are left out, so the list stays about
    what is on screen.
    """
    returned = {segment.segment_id for segment in segments}
    relevant = [
        anomaly
        for anomaly in live.anomalies
        if not anomaly.segment_ids or (returned and returned & set(anomaly.segment_ids))
    ]

    notes: list[str] = []
    if live.coverage.truncated:
        notes.append(
            f"The source ends at {format_timecode(live.coverage.content_end_s)} mid-broadcast; "
            "the end of this live is missing."
        )
    if live.coverage.timestamps_kind == "estimated_interpolated":
        notes.append(
            "Timestamps are interpolated estimates, not measured; treat them as approximate."
        )
    if not live.live.video_url:
        notes.append("This live has no video URL, so no deep links are available.")
    notes.append(
        "Speaker attribution is inferred from context, not verified against audio."
        if any(s.speaker.needs_audio_review for s in live.segments)
        else "Speaker attribution is inferred from context."
    )
    notes.append(
        "The source mention index covers single equities only."
        if live.coverage.mention_index_scope == "equities_only"
        else "The source mention index covers multiple asset classes."
    )
    notes.append("Spoken numbers come from automatic transcription and are not verified.")

    return LiveCaveats(
        live_id=live.live.live_id,
        truncated=live.coverage.truncated,
        mention_index_scope=live.coverage.mention_index_scope,
        timestamps_estimated=live.coverage.timestamps_kind == "estimated_interpolated",
        anomalies=[
            AnomalyNote(
                kind=a.kind, detail=a.detail, segment_ids=list(a.segment_ids), chapter_id=a.chapter_id
            )
            for a in relevant
        ],
        notes=notes,
    )


def guest_speaker_names(store: ContentStore, live: CanonicalLive) -> list[str]:
    """Speakers heading a guest block, in chapter order, without repeats.

    A speaker can head more than one block — Sartorelli heads two in
    MB20261028 — so order is preserved and duplicates dropped.
    """
    names: list[str] = []
    for chapter in live.chapters:
        for speaker_id in chapter.speaker_ids:
            name = store.get_speaker(speaker_id).name
            if name not in names:
                names.append(name)
    return names


def lives_index(store: ContentStore) -> LivesIndex:
    """REQ-0021: the index of lives, most recent first."""
    entries = [
        LiveIndexEntry(
            live_id=live.live.live_id,
            date=live.live.date,
            title=live.live.title,
            programme_id=live.live.programme_id,
            programme_name=store.get_programme(live.live.programme_id).name,
            guest_speaker_names=guest_speaker_names(store, live),
            chapter_count=len(live.chapters),
            segment_count=len(live.segments),
            truncated=live.coverage.truncated,
            has_video=bool(live.live.video_url),
        )
        for live in store.lives()
    ]
    return LivesIndex(lives=entries)


def read_summary(settings: Settings, live_id: str, store: ContentStore) -> str:
    """REQ-0001: serve a pre-generated summary, or fail clearly.

    The server never writes one: a faithful summary reproduces speakers' price
    levels and directional views, which needs compliance review before it is
    served (catalog REQ-0001 notes).
    """
    store.get_live(live_id)  # raises UnknownLive for a bad id
    path = settings.summaries_dir / f"{live_id}.md"
    if not path.exists():
        raise SummaryNotGenerated(
            f"no summary has been generated for {live_id}. Expected a reviewed Markdown file at "
            f"{path}. Summaries are produced and compliance-reviewed outside this server, which "
            "never generates one."
        )
    return path.read_text(encoding="utf-8")


def segments_for(live: CanonicalLive, segment_ids: Sequence[str]) -> list[Segment]:
    """Canonical segments by id, in broadcast order."""
    wanted = set(segment_ids)
    return [segment for segment in live.segments if segment.segment_id in wanted]
