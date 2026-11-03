"""Selections that the prompts embed (REQ-0004 and REQ-0013).

The prompts themselves are rendered in `prompts/templates.py`; this module only
decides *which* segments go in. Keeping the two apart means the selection rules
are testable without MCP and without parsing rendered text (section 2).

Every selection is capped (section 7: "Cap embedded text"), and a cap that bites
is reported, never silent: a summary of the first half of a block that presents
itself as a summary of the block would misstate what the speaker said.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import Settings
from ..ingest.slugs import normalize
from ..ingest.timecodes import TimecodeError, format_timecode, parse_timecode
from ..models.canonical import CanonicalLive, Chapter, Segment
from ..models.outputs import LiveCaveats, SegmentRef, SubjectDiscussionResult
from ..models.registries import SpeakerEntry
from ..store.base import ContentStore, StoreError
from .lives import live_caveats, segment_ref, segments_for
from .rubrics import resolve_rubric
from .search import get_subject_discussion
from .speakers import resolve_speaker


class PortionError(ValueError):
    """A selection that cannot be honoured: missing, malformed, or empty.

    The message says what was wrong and what would work, so a model can retry
    (section 5).
    """


# --- shared capping ---------------------------------------------------------

def cap_segments(
    segments: Sequence[Segment], settings: Settings
) -> tuple[list[Segment], bool]:
    """Keep whole segments, in order, within both caps.

    Segments are never cut mid-text: the prompts promise verbatim quotes, and a
    half segment is a misquote. The first segment is always kept even if it
    alone exceeds the character cap (one MB20261028 segment is ~10,000 chars),
    because an empty selection would be worse than a long one.
    """
    kept: list[Segment] = []
    chars = 0
    for segment in segments:
        too_many = len(kept) >= settings.max_prompt_segments
        too_long = kept and chars + len(segment.text) > settings.max_prompt_chars
        if too_many or too_long:
            return kept, True
        kept.append(segment)
        chars += len(segment.text)
    return kept, False


# --- REQ-0004: summarize_live_portion ---------------------------------------

def parse_seconds(name: str, raw: str | None) -> int | None:
    """A prompt argument holding a time: seconds (`2400`, `2400.5`) or a timecode.

    MCP prompt arguments are strings (section 7), so the number is parsed here.
    `mm:ss` and `h:mm:ss` are accepted too because they are how a person reads a
    time off the broadcast; anything else is an error, not a guess.
    """
    if raw is None or not str(raw).strip():
        return None
    value = str(raw).strip()
    if ":" in value:
        try:
            return parse_timecode(value)
        except TimecodeError as exc:
            raise PortionError(f"{name}={raw!r} is not a valid timecode: {exc}") from exc
    try:
        seconds = float(value)
    except ValueError as exc:
        raise PortionError(
            f"{name}={raw!r} is not a number. Give seconds from broadcast start (e.g. 2400) "
            "or a timecode (e.g. 40:00 or 1:05:30)."
        ) from exc
    if seconds != seconds or seconds < 0:  # NaN or negative
        raise PortionError(f"{name}={raw!r} must be a non-negative number of seconds")
    return int(seconds)


def _chapter_options(live: CanonicalLive) -> str:
    return "; ".join(f"{c.chapter_id} ({c.label_raw})" for c in live.chapters)


def _contains_run(tokens: list[str], wanted: list[str]) -> bool:
    n = len(wanted)
    return n > 0 and any(tokens[i : i + n] == wanted for i in range(len(tokens) - n + 1))


def resolve_chapter(store: ContentStore, live: CanonicalLive, value: str) -> Chapter:
    """A chapter of `live` by id, number, label, rubric, or a run of label words.

    The catalog describes this argument as "chapter label or rubric name", and
    its phrasings name chapters by fragments ("lo Speciale 0DTE"), so a
    contiguous run of words from the label is accepted last. Several matches —
    Sartorelli heads two blocks in MB20261028 — is an error listing them, never
    a pick.
    """
    raw = value.strip()
    chapters = list(live.chapters)
    live_id = live.live.live_id

    # 1. The chapter id, or its number ("3", "C03", "c3").
    for chapter in chapters:
        if chapter.chapter_id.casefold() == raw.casefold():
            return chapter
    digits = raw[1:] if raw[:1] in "cC" else raw
    if digits.isdigit():
        wanted_id = f"{live_id}-C{int(digits):02d}"
        for chapter in chapters:
            if chapter.chapter_id == wanted_id:
                return chapter
        raise PortionError(
            f"{live_id} has no chapter number {int(digits)}. Chapters: {_chapter_options(live)}"
        )

    wanted = normalize(raw)

    # 2. The full label as broadcast.
    exact = [c for c in chapters if normalize(c.label_raw) == wanted]
    if len(exact) == 1:
        return exact[0]

    # 3. A rubric from the registry, by id, name or alias.
    try:
        rubric = resolve_rubric(store, raw)
    except (ValueError, StoreError):
        rubric = None
    if rubric is not None:
        by_rubric = [c for c in chapters if c.rubric_id == rubric.rubric_id]
        if len(by_rubric) == 1:
            return by_rubric[0]
        if not by_rubric:
            raise PortionError(
                f"the rubric {rubric.name!r} does not occur in {live_id}. "
                f"Chapters: {_chapter_options(live)}"
            )
        exact = by_rubric

    # 4. A run of words from the label: a guest's name, or a topic fragment.
    if not exact:
        tokens = wanted.split()
        exact = [c for c in chapters if _contains_run(normalize(c.label_raw).split(), tokens)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise PortionError(
            f"chapter {value!r} matches {len(exact)} chapters of {live_id}: "
            + "; ".join(f"{c.chapter_id} ({c.label_raw})" for c in exact)
            + ". Pass one chapter_id."
        )
    raise PortionError(
        f"no chapter of {live_id} matches {value!r}. Chapters: {_chapter_options(live)}"
    )


def _overlaps(segment: Segment, start: int | None, end: int | None) -> bool:
    """Whether a segment falls in a window, inclusive at both ends.

    A segment ending exactly where the window starts is outside it; a
    zero-duration segment sitting on the start is inside.
    """
    if end is not None and segment.start_s > end:
        return False
    if start is not None and segment.end_s <= start and segment.start_s < start:
        return False
    return True


@dataclass(frozen=True)
class PortionSelection:
    live: CanonicalLive
    selectors: list[str]
    """Plain-language statements of what was asked for, for the prompt header."""

    segments: list[SegmentRef]
    total_selected: int
    capped: bool
    reaches_truncated_end: bool
    caveats: LiveCaveats
    speaker_names: list[str] = field(default_factory=list)


def select_portion(
    store: ContentStore,
    settings: Settings,
    live_id: str,
    chapter: str | None = None,
    speaker: str | None = None,
    start_s: str | None = None,
    end_s: str | None = None,
) -> PortionSelection:
    """REQ-0004: the segments of a live picked out by chapter, speaker and/or time.

    Selectors combine as an intersection. At least one is required — the
    catalog notes that its schema cannot express this, so it is enforced here.
    Summarizing a whole live is what the (reviewed, file-backed) summary
    resource is for.
    """
    live = store.get_live(live_id)

    chapter = chapter.strip() if chapter and chapter.strip() else None
    speaker = speaker.strip() if speaker and speaker.strip() else None
    start = parse_seconds("start_s", start_s)
    end = parse_seconds("end_s", end_s)

    if chapter is None and speaker is None and start is None and end is None:
        raise PortionError(
            "summarize_live_portion needs at least one selector: chapter (label, rubric name "
            "or chapter_id), speaker (id, name or surname), or a time window start_s/end_s "
            f"(seconds or mm:ss). Chapters of {live.live.live_id}: {_chapter_options(live)}. "
            "For a whole-live summary use the easygoing://lives/{live_id}/summary resource."
        )
    if start is not None and end is not None and start > end:
        raise PortionError(f"start_s ({start}) is after end_s ({end})")
    content_end = live.coverage.content_end_s
    if start is not None and start > content_end:
        raise PortionError(
            f"start_s ({start}) is past the end of the transcript of {live.live.live_id}, "
            f"which stops at {content_end} s ({format_timecode(content_end)})"
            + (" because the source is truncated" if live.coverage.truncated else "")
            + "."
        )

    selectors: list[str] = []
    chosen_chapter: Chapter | None = None
    speaker_entry: SpeakerEntry | None = None

    if chapter is not None:
        chosen_chapter = resolve_chapter(store, live, chapter)
        selectors.append(f"chapter {chosen_chapter.chapter_id} ({chosen_chapter.label_raw})")
    if speaker is not None:
        speaker_entry = resolve_speaker(store, speaker)
        selectors.append(f"speaker {speaker_entry.name} ({speaker_entry.speaker_id})")
    if start is not None or end is not None:
        selectors.append(
            "time window "
            f"{format_timecode(start) if start is not None else 'start'}"
            f"–{format_timecode(end) if end is not None else 'end'}"
        )

    selected = [
        s
        for s in live.segments
        if (chosen_chapter is None or s.chapter_id == chosen_chapter.chapter_id)
        and (speaker_entry is None or s.speaker.speaker_id == speaker_entry.speaker_id)
        and _overlaps(s, start, end)
    ]

    if not selected:
        hint = ""
        if speaker_entry is not None:
            speaking = [s for s in live.segments if s.speaker.speaker_id == speaker_entry.speaker_id]
            hint = (
                f" {speaker_entry.name} has no segments in {live.live.live_id}."
                if not speaking
                else f" {speaker_entry.name} speaks in {live.live.live_id} between "
                f"{format_timecode(speaking[0].start_s)} and {format_timecode(speaking[-1].end_s)}."
            )
        raise PortionError(
            f"no segment of {live.live.live_id} matches {' and '.join(selectors)}.{hint}"
        )

    kept, capped = cap_segments(selected, settings)
    last = live.segments[-1].segment_id
    return PortionSelection(
        live=live,
        selectors=selectors,
        segments=[segment_ref(store, live, s) for s in kept],
        total_selected=len(selected),
        capped=capped,
        reaches_truncated_end=live.coverage.truncated and selected[-1].segment_id == last,
        caveats=live_caveats(live, kept),
        speaker_names=sorted({store.get_speaker(s.speaker.speaker_id).name for s in kept}),
    )


# --- REQ-0013: compare_views -----------------------------------------------

def parse_speaker_list(raw: str | None) -> list[str]:
    """`speakers` arrives as one comma-separated string (MCP prompt args are strings)."""
    if raw is None:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class ComparisonMaterial:
    discussion: SubjectDiscussionResult
    """Filtered to the requested speakers and capped."""

    requested: list[SpeakerEntry]
    """Speakers the caller named, resolved. Empty means "everyone who discussed it"."""

    silent: list[SpeakerEntry]
    """Requested speakers who speak in this live but have no segment matching the subject."""

    absent: list[SpeakerEntry]
    """Requested speakers who do not speak in this live at all."""

    dropped_context: bool
    """True when context segments were removed to fit the cap."""

    capped_speakers: list[str]
    """Speakers whose matching segments were also cut to fit the cap."""


def comparison_material(
    store: ContentStore,
    settings: Settings,
    live_id: str,
    subject: str,
    speakers: str | None,
    *,
    disclaimer: str,
) -> ComparisonMaterial:
    """REQ-0013: `get_subject_discussion` output for the speakers being compared.

    Single-live only in Phase 1: comparing across lives needs the stance table
    (REQ-0016, section 8).

    Capping prefers keeping every speaker's matching segments over keeping
    context, and keeps each speaker represented: a comparison that silently lost
    one side would read as agreement.
    """
    requested: list[SpeakerEntry] = []
    for name in parse_speaker_list(speakers):
        entry = resolve_speaker(store, name)
        if entry.speaker_id not in {e.speaker_id for e in requested}:
            requested.append(entry)

    discussion = get_subject_discussion(
        store, settings, live_id, subject, None, disclaimer=disclaimer
    )
    groups = list(discussion.groups)
    if requested:
        wanted = {e.speaker_id for e in requested}
        groups = [g for g in groups if g.speaker_id in wanted]
    live = store.get_live(live_id)
    discussing = {g.speaker_id for g in groups}
    present = {s.speaker.speaker_id for s in live.segments}
    silent = [e for e in requested if e.speaker_id not in discussing and e.speaker_id in present]
    absent = [e for e in requested if e.speaker_id not in present]

    if not groups:
        who = ", ".join(g.speaker_name for g in discussion.groups) or "nobody"
        raise PortionError(
            f"none of the requested speakers ({', '.join(e.name for e in requested)}) discussed "
            f"{subject!r} in {live_id}. Speakers who did: {who}."
        )

    # Cap: first drop context, then share what is left evenly between speakers.
    def total(gs) -> int:
        return sum(len(g.segments) for g in gs)

    def chars(gs) -> int:
        return sum(len(s.text) for g in gs for s in g.segments)

    def fits(gs) -> bool:
        return total(gs) <= settings.max_prompt_segments and chars(gs) <= settings.max_prompt_chars

    dropped_context = False
    capped_speakers: list[str] = []
    if not fits(groups):
        dropped_context = True
        groups = [
            g.model_copy(
                update={"segments": [s for s in g.segments if s.segment_id in set(g.matched_segment_ids)]}
            )
            for g in groups
        ]
    if not fits(groups):
        share_n = max(1, settings.max_prompt_segments // len(groups))
        share_c = max(1, settings.max_prompt_chars // len(groups))
        trimmed = []
        for g in groups:
            kept: list[SegmentRef] = []
            used = 0
            for s in g.segments:
                if kept and (len(kept) >= share_n or used + len(s.text) > share_c):
                    break
                kept.append(s)
                used += len(s.text)
            if len(kept) < len(g.segments):
                capped_speakers.append(g.speaker_name)
            ids = {s.segment_id for s in kept}
            trimmed.append(
                g.model_copy(
                    update={
                        "segments": kept,
                        "matched_segment_ids": [i for i in g.matched_segment_ids if i in ids],
                    }
                )
            )
        groups = trimmed

    # Caveats over what is actually embedded, so anomalies stay about it.
    embedded = segments_for(live, [s.segment_id for g in groups for s in g.segments])
    return ComparisonMaterial(
        discussion=discussion.model_copy(
            update={"groups": groups, "caveats": live_caveats(live, embedded)}
        ),
        requested=requested,
        silent=silent,
        absent=absent,
        dropped_context=dropped_context,
        capped_speakers=capped_speakers,
    )
