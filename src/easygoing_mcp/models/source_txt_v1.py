"""Pydantic models of a parsed `transcript_txt_v1` file.

The .txt format is specified in `docs/transcript_txt_format.md`. Unlike the two
JSON source models, these do not mirror a file's keys: they are the structured
result of `ingest/txt_parse.py`, which is the only place that reads the text.
Every item carries its line number so later errors can point back at the file.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TxtMetadataBlock(_Strict):
    """The optional `---` block at the top of the file (spec section 2).

    Every field is optional here; which ones are required is decided when the
    live-level metadata is resolved, because a missing field may still come
    from a line form or, temporarily, from a companion JSON.
    """

    title: str | None = None
    video_url: str | None = None
    content_end: str | None = None
    truncated: bool | None = None
    mention_index_scope: str | None = None
    timestamps: str | None = None
    guests: list[str] | None = None


class TxtChapter(_Strict):
    line: int
    timecode_raw: str
    start_s: int = Field(ge=0)
    annotation: str | None
    label: str
    """Everything after the time, verbatim: `Davide Biocchi — Fed, MSFT, …`."""

    name: str
    """The part before ` — `: a guest name or a rubric name."""

    topics: str | None


class TxtIndexEntry(_Strict):
    line: int
    raw: str
    name: str
    tickers_raw: str | None
    """Contents of the last balanced parenthesis group, verbatim (`000660 (KRX)`)."""

    first_mention_raw: str | None = None
    """Form `— prima occorrenza T`."""

    stated_segment_count: int | None = None
    stated_speakers: list[str] | None = None
    """Form `| N segmenti | a, b`."""


class TxtSegment(_Strict):
    line: int
    clocks_raw: str
    """The bracketed clock group verbatim: `00:00` or `REC 00:04:42 | YT~ 00:00:00`."""

    start_s: int = Field(ge=0)
    estimated: bool
    """The timeline clock carried `~`."""

    other_clocks: dict[str, str] = Field(default_factory=dict)
    """Non-timeline clocks, label to timecode as written (`{"REC": "00:04:42"}`)."""

    speaker: str
    review_flag: str | None
    """Bracketed text between the name and the colon, without brackets."""

    text: str
    section: str | None
    """The most recent `==== NAME ====` header, verbatim, or None."""

    section_occurrence: int | None = None
    """1 for the first header with this normalized name, 2 for the second, …"""


class TxtDocument(_Strict):
    source_file: str
    metadata: TxtMetadataBlock
    title_line: str
    programme_name: str
    """From the title line, as written (`MARKET BRIEFING`)."""

    date: str
    """ISO date from the title line."""

    video_url: str | None
    """From a `Video:` line."""

    guests: list[str] | None
    """From an `OSPITI…:` line."""

    header_text: list[str]
    """Preamble prose, verbatim, never interpreted."""

    chapters: list[TxtChapter]
    index: list[TxtIndexEntry]
    index_form: str | None
    """`first_mention` or `segment_count`, or None without an index."""

    notes: list[str]
    """`NOTE…` bullets, without the leading `- `."""

    segments: list[TxtSegment] = Field(min_length=1)
