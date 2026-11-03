"""Fixed text used by advice-risk primitives and prompts (section 6).

The disclaimer contains no investment advice and makes no claim about the
content: it states what the data is and is not, which is what a reader needs in
order to weigh an attributed quote.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..ingest.timecodes import format_timecode
from ..models.outputs import LiveCaveats, SegmentRef

DISCLAIMER = (
    "This material reproduces what speakers said during a live broadcast. It is not investment "
    "advice, not a recommendation, and not a solicitation to buy or sell anything. Opinions "
    "belong to the speaker they are attributed to and are reported, not endorsed. Speaker "
    "attribution is inferred from context rather than verified against audio, and spoken figures "
    "come from automatic transcription and have not been checked. Verify anything you intend to "
    "act on against the original broadcast."
)

SUMMARY_HEADER = "> " + DISCLAIMER.replace(". ", ".\n> ")
"""The disclaimer as a Markdown blockquote, for the Markdown-bodied summary."""


# --- Prompt rendering (REQ-0004, REQ-0013) ----------------------------------
#
# Instructions are fixed text: the only variable parts of a prompt are the data
# block (what was selected, its caveats, the segments) and conditional notes
# derived from that data. A prompt never carries a sentence that is true of one
# live and false of another unless it was computed from the live.

SUMMARIZE_INSTRUCTIONS = """\
Summarize the portion of an EasyGoing live broadcast transcribed below.

Rules:
1. Use only the transcript below. Do not add facts, market data, background or anything \
from your own knowledge, and do not fill gaps in what a speaker said.
2. Attribute every opinion, forecast, assessment or view to the speaker who expressed it, \
by name (for example "Sartorelli ritiene che..."). Never restate a speaker's view as fact \
or as your own.
3. Speaker labels are inferred from context, not verified against audio. Where you rely on \
a line marked NEEDS AUDIO REVIEW, say that the attribution of that passage is unconfirmed.
4. Numbers come from automatic speech recognition and are unverified. Reproduce them as \
transcribed, say they are as transcribed, and do not correct, round or compute with them.
5. Cite the timestamp of each key point. A timestamp starting with "~" is an estimate.
6. If the notes below say the selection reaches the truncated end of the source, or that \
it was capped, state that the summary covers an incomplete portion.
7. Do not give investment advice or recommendations. End with the disclaimer below, \
unchanged.
8. Answer in the language of the user's request; the transcript is in Italian.
"""

COMPARE_INSTRUCTIONS = """\
Compare the views of the speakers below on the subject named, within this one live \
broadcast, highlighting where they agree and where they disagree.

Rules:
1. Use only the quoted segments below. Do not add facts, market data or background, and \
do not compare with other broadcasts: this material covers one live only.
2. Support every point with an attributed quote or close citation and its timestamp. \
Never restate a speaker's view as fact or as your own.
3. Lines tagged [match] mention the subject; lines tagged [context] are adjacent talk by the \
same speaker and may be about something else. Do not read a view on the subject into a \
context line that does not express one.
4. Where a speaker's segments do not state a clear position, say "no clear position" rather \
than infer one. Not mentioning something is neither agreement nor disagreement.
5. Do not attribute any view to speakers listed as not having discussed the subject.
6. Speaker labels are inferred from context, not verified against audio. Where a point rests \
on a line marked NEEDS AUDIO REVIEW, say that the attribution is unconfirmed.
7. Numbers come from automatic speech recognition and are unverified. Reproduce them as \
transcribed and do not correct or compute with them; a disagreement that rests only on a \
transcribed number should be flagged as such.
8. Structure: each speaker's view in one or two sentences with a quote; then points of \
agreement; then points of disagreement; then caveats. If only one speaker discussed the \
subject, say that no comparison is possible and report that speaker's view.
9. Do not give investment advice or recommendations. End with the disclaimer below, \
unchanged.
10. Answer in the language of the user's request; the transcript is in Italian.
"""


def segment_line(ref: SegmentRef, tag: str | None = None) -> str:
    """One segment as a prompt line: time, speaker, provenance, then the text verbatim."""
    flags = [ref.segment_id, f"attribution: {ref.attribution_confidence}"]
    if ref.needs_audio_review:
        flags.append("NEEDS AUDIO REVIEW")
    if ref.mixed_speakers:
        flags.append("mixed speakers")
    prefix = f"[{tag}] " if tag else ""
    approx = "~" if ref.timestamp_label.startswith("~") else ""
    span = f"{ref.timestamp_label}–{approx}{format_timecode(ref.end_s)}"
    line = f"{prefix}[{span}] {ref.speaker_name} ({'; '.join(flags)}): {ref.text}"
    if ref.deep_link:
        line += f" <{ref.deep_link}{' approx.' if ref.deep_link_approximate else ''}>"
    return line


def caveat_lines(caveats: LiveCaveats) -> list[str]:
    """Caveats as bullet lines: the plain-language notes, then each anomaly."""
    lines = [f"- {note}" for note in caveats.notes]
    for anomaly in caveats.anomalies:
        where = f" (segments {', '.join(anomaly.segment_ids)})" if anomaly.segment_ids else ""
        lines.append(f"- Anomaly {anomaly.kind}{where}: {anomaly.detail}")
    return lines


def _section(title: str, lines: Iterable[str]) -> str:
    return f"## {title}\n" + "\n".join(lines)


def render_summarize_prompt(selection) -> str:
    """REQ-0004. `selection` is a `services.portions.PortionSelection`."""
    live = selection.live
    refs = selection.segments
    header = [
        f"Live: {live.live.live_id}, {live.live.date}"
        + (f", \"{live.live.title}\"" if live.live.title else ""),
        f"Selected by: {'; '.join(selection.selectors)}",
        f"Speakers in the selection: {', '.join(selection.speaker_names)}",
        f"Span: {refs[0].timestamp_label}–{format_timecode(refs[-1].end_s)}",
        f"Segments embedded: {len(refs)} of {selection.total_selected} selected",
    ]
    notes: list[str] = []
    if selection.capped:
        notes.append(
            f"- CAPPED: only the first {len(refs)} of {selection.total_selected} selected segments "
            f"are embedded, up to {format_timecode(refs[-1].end_s)}. The summary must say it "
            "covers only this part; the rest can be requested with a narrower selection."
        )
    if selection.reaches_truncated_end:
        notes.append(
            "- TRUNCATED END: the selection reaches the point where the source stops "
            f"mid-broadcast, at {format_timecode(live.coverage.content_end_s)}. What follows in "
            "the broadcast is missing."
        )
    audio = sum(1 for r in refs if r.needs_audio_review)
    if audio:
        notes.append(f"- {audio} of the embedded segments are marked NEEDS AUDIO REVIEW.")

    parts = [
        SUMMARIZE_INSTRUCTIONS,
        _section("Selection", header),
        _section("Notes on this selection", notes + caveat_lines(selection.caveats)),
        _section("Transcript", (segment_line(r) for r in refs)),
        _section("Disclaimer", [DISCLAIMER]),
    ]
    return "\n\n".join(parts)


def render_compare_prompt(material) -> str:
    """REQ-0013. `material` is a `services.portions.ComparisonMaterial`."""
    d = material.discussion
    s = d.subject
    header = [
        f"Live: {d.live_id}, {d.date}",
        f"Subject: {s.subject!r}, understood as {s.resolved_as!r} (matched by {s.match_kind})",
        f"Speakers compared: {', '.join(g.speaker_name for g in d.groups)}",
    ]
    if s.variants_used:
        header.append(
            "Also searched for: " + ", ".join(repr(v) for v in s.variants_used)
            + " (other names for the same subject)."
        )

    notes: list[str] = []
    if s.match_kind == "text":
        notes.append(
            "- The subject is not in the instrument registry, so matches come from the transcript "
            "text; some may be incidental mentions."
        )
    if material.silent:
        notes.append(
            "- Requested but did not discuss the subject in this live (no segment matched): "
            + ", ".join(e.name for e in material.silent)
            + "."
        )
    if material.absent:
        notes.append(
            "- Requested but not speaking in this live at all: "
            + ", ".join(e.name for e in material.absent)
            + "."
        )
    if len(d.groups) == 1:
        notes.append("- Only one speaker discussed the subject: no comparison is possible.")
    if material.dropped_context:
        notes.append("- CAPPED: context lines were removed to fit; only [match] lines remain.")
    if material.capped_speakers or d.truncated_groups:
        names = sorted(set(material.capped_speakers) | {
            g.speaker_name for g in d.groups if g.speaker_id in d.truncated_groups
        })
        notes.append(
            "- CAPPED: some segments were left out for " + ", ".join(names)
            + "; the comparison covers only what is shown."
        )

    blocks: list[str] = []
    for g in d.groups:
        matched = set(g.matched_segment_ids)
        head = (
            f"### {g.speaker_name} ({g.speaker_id}): {len(matched)} matching segment(s), "
            f"attribution {', '.join(g.attribution_confidence_values)}"
            + ("; includes segments NEEDING AUDIO REVIEW" if g.any_needs_audio_review else "")
        )
        lines = [
            segment_line(ref, "match" if ref.segment_id in matched else "context")
            for ref in g.segments
        ]
        blocks.append(head + "\n" + "\n".join(lines))

    parts = [
        COMPARE_INSTRUCTIONS,
        _section("Material", header),
        _section("Notes on this material", notes + caveat_lines(d.caveats)),
        "## Quoted segments, by speaker\n\n" + "\n\n".join(blocks),
        _section("Disclaimer", [d.disclaimer or DISCLAIMER]),
    ]
    return "\n\n".join(parts)
