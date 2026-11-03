"""Subject search across and within lives (REQ-0005/0006 and REQ-0007).

Lists are bounded here, not in the server: `limit` is clamped to the configured
maximum and paging uses an opaque cursor, so no caller can ask for an unbounded
segment list (section 5).
"""

from __future__ import annotations

import base64
import binascii
import json
from collections import OrderedDict

from ..config import Settings
from ..models.canonical import CanonicalLive, Segment
from ..models.outputs import (
    FindMentionsResult,
    LiveMentionGroup,
    SpeakerDiscussionGroup,
    SubjectDiscussionResult,
    SubjectResolution,
)
from ..store.base import ContentStore, SegmentQuery
from .lives import live_caveats, segment_ref
from .speakers import resolve_speaker
from .subjects import ResolvedSubject, resolve_subject, subject_query


class InvalidCursor(ValueError):
    """A cursor that did not come from this server."""


def encode_cursor(offset: int) -> str:
    """An opaque page token (section 5).

    Opaque rather than a bare number so the paging scheme can change — an
    offset is right for a linear scan but a keyset cursor suits SQL — without
    the wire format becoming a promise.
    """
    raw = json.dumps({"o": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if cursor is None or cursor == "":
        return 0
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        offset = int(payload["o"])
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise InvalidCursor(
            f"cursor {cursor!r} is not a cursor this server issued. Omit it to start from the "
            "first page, or pass back the next_cursor from a previous response."
        ) from exc
    if offset < 0:
        raise InvalidCursor(f"cursor {cursor!r} decodes to a negative offset")
    return offset


def clamp_limit(limit: int | None, settings: Settings) -> int:
    """Default 10, hard maximum from config (section 5)."""
    if limit is None:
        return settings.default_limit
    return max(1, min(int(limit), settings.max_limit))


def _resolution(resolved: ResolvedSubject) -> SubjectResolution:
    return SubjectResolution(
        subject=resolved.subject,
        resolved_as=resolved.label,
        match_kind=resolved.match_kind,
        instrument_ids=list(resolved.instrument_ids),
        terms_searched=list(resolved.terms),
        variants_used=list(resolved.variants_used),
    )


def _subject_notes(resolved: ResolvedSubject, comparable: bool, scopes: set[str]) -> list[str]:
    notes: list[str] = []
    if resolved.variants_used:
        notes.append(
            f"Also searched for {', '.join(repr(v) for v in resolved.variants_used)}, "
            f"which name the same subject as {resolved.subject!r}."
        )
    if resolved.match_kind == "text":
        notes.append(
            "This subject is not in the instrument registry, so results come from matching the "
            "transcript text rather than the source mention index."
        )
    if not comparable:
        notes.append(
            "The lives covered index mentions at different scopes ("
            + ", ".join(sorted(scopes))
            + "), so counts are not comparable between them."
        )
    return notes


def find_mentions(
    store: ContentStore,
    settings: Settings,
    subject: str,
    live_id: str | None = None,
    speaker: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> FindMentionsResult:
    """REQ-0005 and REQ-0006: where a subject comes up, in one live or across all.

    One tool serves both catalog entries: omitting `live_id` searches every
    live. Results are grouped by live, most recent first, and ordered by start
    time inside each group.
    """
    if live_id is not None:
        store.get_live(live_id)  # validate early, with a helpful message
    speaker_entry = resolve_speaker(store, speaker) if speaker else None

    resolved = resolve_subject(store, subject)
    page_limit = clamp_limit(limit, settings)
    offset = decode_cursor(cursor)

    query = subject_query(
        resolved,
        live_ids=[live_id] if live_id else None,
        speaker_ids=[speaker_entry.speaker_id] if speaker_entry else None,
        date_from=date_from,
        date_to=date_to,
        limit=page_limit,
        offset=offset,
    )
    page = store.search_segments(query)

    grouped: OrderedDict[str, list[Segment]] = OrderedDict()
    for hit in page.items:
        grouped.setdefault(hit.live_id, []).append(hit.segment)

    # Comparability is a property of the whole result, not of this page: if it
    # were computed from the page, a result spanning both lives would report
    # itself comparable on any page that happened to contain only one of them.
    all_live_ids = {
        hit.live_id for hit in store.search_segments(query.without_window()).items
    }
    scopes = {
        store.get_live(matched_live_id).coverage.mention_index_scope
        for matched_live_id in all_live_ids
    }

    groups: list[LiveMentionGroup] = []
    for hit_live_id, segments in grouped.items():
        live = store.get_live(hit_live_id)
        groups.append(
            LiveMentionGroup(
                live_id=hit_live_id,
                date=live.live.date,
                title=live.live.title,
                segments=[segment_ref(store, live, s) for s in segments],
                caveats=live_caveats(live, segments),
            )
        )

    comparable = len(scopes) <= 1
    return FindMentionsResult(
        subject=_resolution(resolved),
        total_matches=page.total,
        returned=len(page.items),
        next_cursor=encode_cursor(page.next_offset) if page.next_offset is not None else None,
        groups=groups,
        comparable_across_lives=comparable,
        notes=_subject_notes(resolved, comparable, scopes),
    )


def _context_for(
    store: ContentStore,
    live: CanonicalLive,
    matched: Segment,
    radius: int,
) -> list[Segment]:
    """Adjacent segments of the same speaker in the same chapter (section 7).

    Both constraints matter: crossing a chapter boundary would attach another
    rubric's talk to this one, and crossing a speaker boundary would put someone
    else's words inside this speaker's group.
    """
    window = store.adjacent_segments(
        live.live.live_id, matched.segment_id, before=radius, after=radius
    )
    return [
        segment
        for segment in window
        if segment.speaker.speaker_id == matched.speaker.speaker_id
        and segment.chapter_id == matched.chapter_id
    ]


def get_subject_discussion(
    store: ContentStore,
    settings: Settings,
    live_id: str,
    subject: str,
    speaker: str | None = None,
    *,
    disclaimer: str = "",
) -> SubjectDiscussionResult:
    """REQ-0007: what each speaker said about a subject in one live.

    Same retrieval as `find_mentions`, grouped by speaker and expanded with
    contiguous context so a quote is not cut mid-argument. Every group is capped,
    so context expansion can never produce an unbounded list (section 5).
    """
    live = store.get_live(live_id)
    speaker_entry = resolve_speaker(store, speaker) if speaker else None
    resolved = resolve_subject(store, subject)

    query = subject_query(
        resolved,
        live_ids=[live_id],
        speaker_ids=[speaker_entry.speaker_id] if speaker_entry else None,
        limit=None,
    )
    page = store.search_segments(query)

    matched_by_speaker: OrderedDict[str, list[Segment]] = OrderedDict()
    for hit in page.items:
        matched_by_speaker.setdefault(hit.segment.speaker.speaker_id, []).append(hit.segment)

    groups: list[SpeakerDiscussionGroup] = []
    capped: list[str] = []
    covered: list[Segment] = []

    for speaker_id, matches in matched_by_speaker.items():
        with_context: dict[str, Segment] = {}
        for matched in matches:
            for segment in _context_for(store, live, matched, settings.context_radius):
                with_context[segment.segment_id] = segment
            with_context[matched.segment_id] = matched

        ordered = sorted(with_context.values(), key=lambda s: (s.start_s, s.segment_id))
        if len(ordered) > settings.max_discussion_segments:
            ordered = ordered[: settings.max_discussion_segments]
            capped.append(speaker_id)

        kept = {s.segment_id for s in ordered}
        covered.extend(ordered)
        groups.append(
            SpeakerDiscussionGroup(
                speaker_id=speaker_id,
                speaker_name=store.get_speaker(speaker_id).name,
                attribution_confidence_values=sorted({s.speaker.confidence for s in ordered}),
                any_needs_audio_review=any(s.speaker.needs_audio_review for s in ordered),
                segments=[segment_ref(store, live, s) for s in ordered],
                matched_segment_ids=[m.segment_id for m in matches if m.segment_id in kept],
            )
        )

    # Speakers in the order they first raise the subject.
    groups.sort(key=lambda g: min(s.start_s for s in g.segments) if g.segments else 0)

    return SubjectDiscussionResult(
        live_id=live.live.live_id,
        date=live.live.date,
        subject=_resolution(resolved),
        groups=groups,
        total_matches=page.total,
        truncated_groups=capped,
        caveats=live_caveats(live, covered),
        disclaimer=disclaimer,
    )
