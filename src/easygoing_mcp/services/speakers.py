"""Speaker registry projections (REQ-0011)."""

from __future__ import annotations

import difflib
from collections import defaultdict

from ..ingest.slugs import normalize, slugify
from ..models.outputs import (
    InstrumentView,
    SpeakerCoverageResult,
    SpeakerLiveAppearance,
    SpeakerView,
)
from ..models.registries import SpeakerEntry
from ..store.base import ContentStore, UnknownSpeaker
from .lives import live_caveats, segment_ref

UNCURATED_NOTE = (
    "Role, affiliation, links and disclosures are not yet curated for this speaker. "
    "They are empty because nobody has filled them in, not because the speaker has none. "
    "These fields are never inferred from the transcripts."
)

CURATED_NOTE = (
    "Role, affiliation, links and disclosures come from the curated speaker registry, "
    "not from the transcripts. Fields still empty are simply unfilled."
)


def speaker_view(store: ContentStore, speaker_id: str) -> SpeakerView:
    """REQ-0011: a speaker's registry entry and the lives they appear in.

    Only registry fields are returned. The response states in words that
    uncurated fields are unfilled, so a null is never read as a finding about
    the person (section 7).
    """
    entry = store.get_speaker(speaker_id)

    appearances: list[SpeakerLiveAppearance] = []
    for live_id in store.live_ids_for_speaker(speaker_id):
        live = store.get_live(live_id)
        appearances.append(
            SpeakerLiveAppearance(
                live_id=live_id,
                date=live.live.date,
                title=live.live.title,
                segment_count=sum(
                    1 for s in live.segments if s.speaker.speaker_id == speaker_id
                ),
                chapter_ids=[
                    c.chapter_id for c in live.chapters if speaker_id in c.speaker_ids
                ],
            )
        )

    return SpeakerView(
        speaker_id=entry.speaker_id,
        name=entry.name,
        aliases=list(entry.aliases),
        role=entry.role,
        affiliation=entry.affiliation,
        links=list(entry.links),
        disclosures=list(entry.disclosures),
        is_curated=entry.is_curated,
        curation_note=CURATED_NOTE if entry.is_curated else UNCURATED_NOTE,
        appearances=appearances,
    )


def _contains_run(tokens: list[str], wanted: list[str]) -> bool:
    """Whether `wanted` appears as a contiguous run inside `tokens`."""
    if not wanted or len(wanted) > len(tokens):
        return False
    return any(
        tokens[i : i + len(wanted)] == wanted for i in range(len(tokens) - len(wanted) + 1)
    )


class AmbiguousSpeaker(ValueError):
    """A speaker reference matching more than one person. Never guessed."""

    def __init__(self, requested: str, candidates: list[SpeakerEntry]) -> None:
        self.requested = requested
        self.candidates = candidates
        listed = ", ".join(f"{c.name!r} (speaker_id={c.speaker_id})" for c in candidates)
        super().__init__(
            f"speaker {requested!r} matches several people: {listed}. Ask again with one of "
            "those names or speaker ids."
        )


def resolve_speaker(store: ContentStore, value: str) -> SpeakerEntry:
    """Resolve a speaker id, full name, alias or surname to a registry entry.

    Catalog phrasings ask things like "Cosa pensa Braganti dell'oro?", so a
    surname on its own has to work. A surname shared by two speakers is an
    error listing both, never a guess (section 5).
    """
    if not value or not value.strip():
        raise ValueError("speaker is empty")

    wanted = normalize(value)
    entries = list(store.speakers())

    for entry in entries:
        if entry.speaker_id == value or entry.speaker_id == slugify(value):
            return entry

    exact = [e for e in entries if normalize(e.name) == wanted]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AmbiguousSpeaker(value, exact)

    aliased = [e for e in entries if any(normalize(a) == wanted for a in e.aliases)]
    if len(aliased) == 1:
        return aliased[0]
    if len(aliased) > 1:
        raise AmbiguousSpeaker(value, aliased)

    # Part of a name, e.g. a surname. Matched as a contiguous run of tokens, not
    # a single one: several speakers here have two-word surnames ("Di Fazio",
    # "Cioli Puviani"), which a token-equality test would never find.
    wanted_tokens = wanted.split()
    partial = [e for e in entries if _contains_run(normalize(e.name).split(), wanted_tokens)]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise AmbiguousSpeaker(value, partial)

    # Nothing matched. The error lists the valid ids, and names a close name if
    # one exists, since a misspelt surname is the likeliest way to get here.
    close = difflib.get_close_matches(value, [e.name for e in entries], n=3, cutoff=0.6)
    error = UnknownSpeaker(value, [e.speaker_id for e in entries])
    if close:
        raise UnknownSpeaker(
            f"{value} (closest names: {', '.join(close)})", [e.speaker_id for e in entries]
        ) from None
    raise error


def speaker_coverage(store: ContentStore, live_id: str, speaker: str) -> SpeakerCoverageResult:
    """REQ-0010: what one speaker covered in one live.

    Instruments come from mentions falling in this speaker's own segments, and
    topics from the `topics_raw` of chapters that list them. Both are reported
    as-is: `topics_raw` is free text as broadcast, never parsed into a taxonomy
    (section 4.4), and mention counts carry the live's index scope because they
    are not comparable with the other live.
    """
    live = store.get_live(live_id)
    entry = resolve_speaker(store, speaker)

    own = [s for s in live.segments if s.speaker.speaker_id == entry.speaker_id]
    own_ids = {s.segment_id for s in own}
    by_segment = {s.segment_id: s for s in live.segments}

    segments_by_instrument: dict[str, list[str]] = defaultdict(list)
    for mention in live.mentions:
        if mention.segment_id in own_ids:
            segments_by_instrument[mention.instrument_id].append(mention.segment_id)

    instruments: list[InstrumentView] = []
    for instrument_id, segment_ids in segments_by_instrument.items():
        registry_entry = store.get_instrument(instrument_id)
        segments = sorted(
            (by_segment[sid] for sid in segment_ids), key=lambda s: (s.start_s, s.segment_id)
        )
        instruments.append(
            InstrumentView(
                instrument_id=instrument_id,
                name=registry_entry.name,
                tickers=list(registry_entry.tickers),
                asset_class=registry_entry.asset_class,
                mention_count=len(segment_ids),
                speaker_ids=[entry.speaker_id],
                speaker_names=[entry.name],
                first_mention=segment_ref(store, live, segments[0]),
            )
        )
    instruments.sort(key=lambda v: (-v.mention_count, v.name))

    chapters = [c for c in live.chapters if entry.speaker_id in c.speaker_ids]
    topics = [c.topics_raw for c in chapters if c.topics_raw]

    return SpeakerCoverageResult(
        live_id=live.live.live_id,
        date=live.live.date,
        speaker_id=entry.speaker_id,
        speaker_name=entry.name,
        segment_count=len(own),
        instruments=instruments,
        topics_raw=topics,
        chapter_ids=[c.chapter_id for c in chapters],
        mention_index_scope=live.coverage.mention_index_scope,
        caveats=live_caveats(live, own),
    )
