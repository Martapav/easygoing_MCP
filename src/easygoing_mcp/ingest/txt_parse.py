"""Parser for the `transcript_txt_v1` plain-text format.

The grammar is specified in `docs/transcript_txt_format.md`; this module is its
only reader. It accepts the documented variants (bare or labelled clocks,
column or pipe chapter lists, two index forms) and nothing else. A line that
matches no rule raises `TxtFormatError` with its line number. Nothing is
guessed and no line is skipped silently (CLAUDE.md section 2).

Parsing is purely syntactic. Live-level metadata is resolved in
`txt_metadata.py`, and canonical conversion happens in `transcript_txt_v1.py`.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from ..models.source_txt_v1 import (
    TxtChapter,
    TxtDocument,
    TxtIndexEntry,
    TxtMetadataBlock,
    TxtSegment,
)
from .slugs import normalize
from .timecodes import TimecodeError, parse_timecode


class TxtFormatError(ValueError):
    """The text breaks the documented grammar. The message names file and line."""


_TC = r"\d{1,2}(?::\d{2}){1,2}"

_FILENAME_RE = re.compile(r"^(?P<programme>[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*?)_(?P<date>\d{4}-\d{2}-\d{2})(?:_.*)?\.txt$")
_TITLE_RE = re.compile(
    r"^\s*(?P<programme>.+?)\s+(?:—|–|-)\s+(?P<day>\d{1,2})\s+(?P<month>[^\W\d_]+)\s+(?P<year>\d{4})\s*$"
)
_MONTHS_IT = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}

_SEGMENT_RE = re.compile(
    r"^\[(?P<clocks>[^\]]+)\]\s+(?P<speaker>[^\[\]:]+?)\s*(?:\[(?P<flag>[^\]]*)\]\s*)?:\s?(?P<text>.*)$"
)
_BARE_CLOCK_RE = re.compile(rf"^(?P<tilde>~)?(?P<tc>{_TC})$")
_LABELLED_CLOCK_RE = re.compile(rf"^(?P<label>[A-Za-z]+)(?P<tilde>~)?\s+(?P<tc>{_TC})$")
_TIMELINE_LABEL = "YT"

_SECTION_RE = re.compile(r"^\s*={2,}\s*(?P<name>[^=\s][^=]*?)\s*={2,}\s*$")
_SEPARATOR_RE = re.compile(r"^\s*(?:={3,}|-{3,})\s*$")

_CHAPTER_RE = re.compile(
    rf"^\s*(?P<tc>{_TC})(?:\s*\[(?P<ann>[^\]]*)\])?\s*(?:\|\s*|\s+)(?P<label>\S.*?)\s*$"
)
_CHAPTER_SEPARATOR = " — "

_INDEX_FIRST_RE = re.compile(
    rf"^\s*(?P<head>.+?)\s+(?:—|–|-)\s+prima occorrenza\s+(?P<tc>{_TC})\s*$", re.IGNORECASE
)
_INDEX_COUNT_RE = re.compile(
    r"^\s*(?P<head>[^|]+?)\s*\|\s*(?P<n>\d+)\s+segment[oi]\s*\|\s*(?P<speakers>.*?)\s*$",
    re.IGNORECASE,
)

_VIDEO_RE = re.compile(r"^\s*video\s*:\s*(?P<url>\S+)\s*$", re.IGNORECASE)
_GUESTS_RE = re.compile(r"^\s*OSPITI\b[^:]*:\s*(?P<names>.+?)\s*\.?\s*$")
_BULLET_RE = re.compile(r"^\s*-\s+(?P<text>\S.*?)\s*$")
_HEADING_WORD_RE = re.compile(r"^\s*(?P<word>[^\s:(]+)")

# Heading keywords (spec section 4): first word in capitals, accent-insensitive.
_HEADINGS = {"capitoli": "chapters", "titoli": "index", "indice": "index", "note": "notes"}

_METADATA_KEYS = frozenset(TxtMetadataBlock.model_fields)


def _heading(line: str) -> str | None:
    """The block a heading line opens, or None if the line is not a heading."""
    match = _HEADING_WORD_RE.match(line)
    if match is None:
        return None
    word = match.group("word")
    if word != word.upper() or not any(ch.isalpha() for ch in word):
        return None
    folded = normalize(word)
    for keyword, block in _HEADINGS.items():
        if folded.startswith(keyword):
            return block
    return None


def _split_head(head: str) -> tuple[str, str | None]:
    """`SK hynix (000660 (KRX))` -> (`SK hynix`, `000660 (KRX)`).

    Tickers are the last balanced parenthesis group; inner parentheses are kept.
    """
    head = head.strip()
    if not head.endswith(")"):
        return head, None
    depth = 0
    for position in range(len(head) - 1, -1, -1):
        if head[position] == ")":
            depth += 1
        elif head[position] == "(":
            depth -= 1
            if depth == 0:
                return head[:position].strip(), head[position + 1 : -1].strip() or None
    return head, None


def txt_signature(text: str) -> bool:
    """True when the text has a title line and at least one segment line.

    Used by `detect.py` to claim a .txt file before parsing it strictly.
    """
    lines = text.lstrip("﻿").splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        try:
            start = lines.index("---", 1) + 1
        except ValueError:
            return False
    title = next((line for line in lines[start:] if line.strip()), "")
    return bool(_TITLE_RE.match(title)) and any(_SEGMENT_RE.match(line) for line in lines)


class _Parser:
    def __init__(self, text: str, source_file: str) -> None:
        self.source_file = source_file
        self.lines = text.lstrip("﻿").splitlines()

        self.metadata = TxtMetadataBlock()
        self.title_line = ""
        self.programme_name = ""
        self.date = ""
        self.video_url: str | None = None
        self.guests: list[str] | None = None
        self.header_text: list[str] = []
        self.chapters: list[TxtChapter] = []
        self.index: list[TxtIndexEntry] = []
        self.notes: list[str] = []
        self.segments: list[TxtSegment] = []

    # -- errors -------------------------------------------------------------

    def fail(self, line_no: int, message: str) -> TxtFormatError:
        if 1 <= line_no <= len(self.lines):
            shown = self.lines[line_no - 1].strip()
            if len(shown) > 100:
                shown = shown[:100] + "…"
            return TxtFormatError(f"{self.source_file}:{line_no}: {message}. Line: {shown!r}")
        return TxtFormatError(f"{self.source_file}: {message}")

    # -- sections of the file ---------------------------------------------

    def parse(self) -> TxtDocument:
        position = self._metadata_block(0)
        position = self._title(position)

        body_start = next(
            (i for i in range(position, len(self.lines)) if self._is_body_line(self.lines[i])),
            None,
        )
        if body_start is None:
            raise self.fail(0, "no segment line found (expected `[time] Speaker: text`)")
        body_end = max(i for i in range(body_start, len(self.lines)) if self.lines[i].startswith("["))

        self._blocks(position, body_start, allow_prose=True)
        self._body(body_start, body_end)
        self._blocks(body_end + 1, len(self.lines), allow_prose=False)
        self._check_order()

        try:
            return TxtDocument(
                source_file=self.source_file,
                metadata=self.metadata,
                title_line=self.title_line,
                programme_name=self.programme_name,
                date=self.date,
                video_url=self.video_url,
                guests=self.guests,
                header_text=self.header_text,
                chapters=self.chapters,
                index=self.index,
                index_form=self._index_form(),
                notes=self.notes,
                segments=self.segments,
            )
        except ValidationError as exc:
            raise TxtFormatError(f"{self.source_file}: {exc}") from exc

    def _metadata_block(self, position: int) -> int:
        if not self.lines or self.lines[0].strip() != "---":
            return position
        values: dict[str, object] = {}
        for i in range(1, len(self.lines)):
            line = self.lines[i]
            if line.strip() == "---":
                try:
                    self.metadata = TxtMetadataBlock(**values)
                except ValidationError as exc:
                    raise self.fail(i + 1, f"invalid metadata block: {exc.errors()[0]['msg']}") from exc
                return i + 1
            if not line.strip():
                continue
            key, sep, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if not sep or key not in _METADATA_KEYS:
                raise self.fail(
                    i + 1, f"unknown metadata key {key!r}; allowed: {sorted(_METADATA_KEYS)}"
                )
            if key in values:
                raise self.fail(i + 1, f"metadata key {key!r} given twice")
            values[key] = self._metadata_value(i + 1, key, value)
        raise self.fail(1, "metadata block opened with `---` is never closed")

    def _metadata_value(self, line_no: int, key: str, value: str) -> object:
        if not value:
            return None
        if key == "truncated":
            lowered = value.lower()
            if lowered not in {"true", "false"}:
                raise self.fail(line_no, f"truncated must be true or false, got {value!r}")
            return lowered == "true"
        if key == "guests":
            return [name.strip() for name in value.split(",") if name.strip()]
        if key == "content_end":
            try:
                parse_timecode(value)
            except TimecodeError as exc:
                raise self.fail(line_no, str(exc)) from exc
        return value

    def _title(self, position: int) -> int:
        while position < len(self.lines) and not self.lines[position].strip():
            position += 1
        if position >= len(self.lines):
            raise self.fail(0, "empty file")
        line = self.lines[position]
        match = _TITLE_RE.match(line)
        if match is None:
            raise self.fail(position + 1, "expected a title line `PROGRAMME — DD MESE YYYY`")
        month = _MONTHS_IT.get(normalize(match.group("month")))
        if month is None:
            raise self.fail(
                position + 1, f"unknown Italian month {match.group('month')!r}"
            )
        self.title_line = line.strip()
        self.programme_name = match.group("programme").strip()
        self.date = f"{int(match.group('year')):04d}-{month:02d}-{int(match.group('day')):02d}"

        name_match = _FILENAME_RE.match(Path(self.source_file).name)
        if name_match is None:
            raise self.fail(
                0, "file name must be `<Programme_Name>_<YYYY-MM-DD>_<suffix>.txt`"
            )
        if name_match.group("date") != self.date:
            raise self.fail(
                position + 1,
                f"title line date {self.date} disagrees with file name date {name_match.group('date')}",
            )
        if normalize(name_match.group("programme").replace("_", " ")) != normalize(self.programme_name):
            raise self.fail(
                position + 1,
                f"title line programme {self.programme_name!r} disagrees with file name "
                f"programme {name_match.group('programme')!r}",
            )
        return position + 1

    @staticmethod
    def _is_body_line(line: str) -> bool:
        return line.startswith("[") or bool(_SECTION_RE.match(line))

    # -- preamble and trailing blocks ---------------------------------------

    def _blocks(self, start: int, stop: int, *, allow_prose: bool) -> None:
        mode: str | None = None
        for i in range(start, stop):
            line, line_no = self.lines[i], i + 1
            if not line.strip():
                mode = None
                continue
            if _SEPARATOR_RE.match(line):
                mode = None
                continue

            if mode == "chapters" and _CHAPTER_RE.match(line):
                self._chapter(line, line_no)
                continue
            if mode == "index" and (_INDEX_FIRST_RE.match(line) or _INDEX_COUNT_RE.match(line)):
                self._index_entry(line, line_no)
                continue
            if mode == "notes" and _BULLET_RE.match(line):
                self.notes.append(_BULLET_RE.match(line).group("text"))  # type: ignore[union-attr]
                continue

            block = _heading(line)
            if block is not None:
                if block == "chapters" and not allow_prose:
                    raise self.fail(line_no, "a chapter list must come before the transcript")
                mode = block
                continue

            if mode is not None:
                expected = {
                    "chapters": "a chapter line `<time> [|] Label — topics`",
                    "index": "an index line `Name (TICKER) — prima occorrenza T` or `Name (TICKER) | N segmenti | speakers`",
                    "notes": "a `- ` bullet",
                }[mode]
                raise self.fail(line_no, f"expected {expected}, or a blank line to close the block")

            if allow_prose:
                if (video := _VIDEO_RE.match(line)) is not None:
                    self._set_once(line_no, "video_url", video.group("url"))
                    continue
                if (guests := _GUESTS_RE.match(line)) is not None:
                    names = [n.strip() for n in guests.group("names").split(",") if n.strip()]
                    self._set_once(line_no, "guests", names)
                    continue
                if line.lstrip().startswith("["):
                    raise self.fail(line_no, "line starts with `[` but is not a valid segment line")
                self.header_text.append(line.strip())
                continue

            raise self.fail(
                line_no,
                "unexpected line after the transcript; only an index block, a NOTE block, "
                "blank lines and separators may follow the last segment",
            )

    def _set_once(self, line_no: int, attribute: str, value: object) -> None:
        if getattr(self, attribute) is not None:
            raise self.fail(line_no, f"{attribute} stated twice")
        setattr(self, attribute, value)

    def _chapter(self, line: str, line_no: int) -> None:
        match = _CHAPTER_RE.match(line)
        assert match is not None
        try:
            start_s = parse_timecode(match.group("tc"))
        except TimecodeError as exc:
            raise self.fail(line_no, str(exc)) from exc
        label = match.group("label")
        if _CHAPTER_SEPARATOR in label:
            name, topics = label.split(_CHAPTER_SEPARATOR, 1)
            name, topics = name.strip(), topics.strip() or None
        else:
            name, topics = label.strip(), None
        annotation = match.group("ann")
        self.chapters.append(
            TxtChapter(
                line=line_no,
                timecode_raw=match.group("tc"),
                start_s=start_s,
                annotation=annotation.strip() if annotation is not None else None,
                label=label,
                name=name,
                topics=topics,
            )
        )

    def _index_entry(self, line: str, line_no: int) -> None:
        if (match := _INDEX_FIRST_RE.match(line)) is not None:
            name, tickers = _split_head(match.group("head"))
            try:
                parse_timecode(match.group("tc"))
            except TimecodeError as exc:
                raise self.fail(line_no, str(exc)) from exc
            entry = TxtIndexEntry(
                line=line_no, raw=line.strip(), name=name, tickers_raw=tickers,
                first_mention_raw=match.group("tc"),
            )
        else:
            match = _INDEX_COUNT_RE.match(line)
            assert match is not None
            name, tickers = _split_head(match.group("head"))
            speakers = [s.strip() for s in match.group("speakers").split(",") if s.strip()]
            entry = TxtIndexEntry(
                line=line_no, raw=line.strip(), name=name, tickers_raw=tickers,
                stated_segment_count=int(match.group("n")), stated_speakers=speakers,
            )
        if not entry.name:
            raise self.fail(line_no, "index entry has no name")
        self.index.append(entry)

    def _index_form(self) -> str | None:
        forms = {
            "first_mention" if entry.first_mention_raw is not None else "segment_count"
            for entry in self.index
        }
        if len(forms) > 1:
            raise TxtFormatError(
                f"{self.source_file}: the index mixes the `prima occorrenza` and `N segmenti` forms"
            )
        return forms.pop() if forms else None

    # -- transcript body ------------------------------------------------------

    def _body(self, start: int, stop: int) -> None:
        section: str | None = None
        occurrence: int | None = None
        seen_sections: Counter[str] = Counter()

        for i in range(start, stop + 1):
            line, line_no = self.lines[i], i + 1
            if not line.strip() or _SEPARATOR_RE.match(line):
                continue
            if (header := _SECTION_RE.match(line)) is not None:
                section = header.group("name")
                seen_sections[normalize(section)] += 1
                occurrence = seen_sections[normalize(section)]
                continue
            match = _SEGMENT_RE.match(line)
            if match is None:
                raise self.fail(
                    line_no,
                    "inside the transcript only `[time] Speaker[ [flag]]: text` lines, "
                    "`==== SECTION ====` headers, separators and blank lines are allowed",
                )
            start_s, estimated, others = self._clocks(match.group("clocks"), line_no)
            flag = match.group("flag")
            self.segments.append(
                TxtSegment(
                    line=line_no,
                    clocks_raw=match.group("clocks").strip(),
                    start_s=start_s,
                    estimated=estimated,
                    other_clocks=others,
                    speaker=match.group("speaker").strip(),
                    review_flag=flag.strip() if flag is not None else None,
                    text=match.group("text"),
                    section=section,
                    section_occurrence=occurrence,
                )
            )

    def _clocks(self, raw: str, line_no: int) -> tuple[int, bool, dict[str, str]]:
        parts = [part.strip() for part in raw.split("|")]
        try:
            if len(parts) == 1 and (bare := _BARE_CLOCK_RE.match(parts[0])) is not None:
                return parse_timecode(bare.group("tc")), bare.group("tilde") is not None, {}

            timeline: tuple[int, bool] | None = None
            others: dict[str, str] = {}
            for part in parts:
                labelled = _LABELLED_CLOCK_RE.match(part)
                if labelled is None:
                    raise self.fail(
                        line_no, f"clock {part!r} is neither `mm:ss`/`h:mm:ss` nor `LABEL time`"
                    )
                label = labelled.group("label").upper()
                if label == _TIMELINE_LABEL:
                    if timeline is not None:
                        raise self.fail(line_no, "two YT clocks in one clock group")
                    timeline = (parse_timecode(labelled.group("tc")), labelled.group("tilde") is not None)
                else:
                    if label in others:
                        raise self.fail(line_no, f"clock label {label!r} repeated")
                    parse_timecode(labelled.group("tc"))
                    others[label] = labelled.group("tc")
        except TimecodeError as exc:
            raise self.fail(line_no, str(exc)) from exc

        if timeline is None:
            raise self.fail(line_no, "labelled clocks need one labelled `YT` (the YouTube timeline)")
        return timeline[0], timeline[1], others

    # -- whole-file checks ----------------------------------------------------

    def _check_order(self) -> None:
        for previous, current in zip(self.segments, self.segments[1:]):
            if current.start_s < previous.start_s:
                raise self.fail(
                    current.line,
                    f"segment time {current.start_s} s precedes the previous segment ({previous.start_s} s)",
                )
        for previous, current in zip(self.chapters, self.chapters[1:]):
            if current.start_s < previous.start_s:
                raise self.fail(current.line, "chapter times must not decrease")
        estimated = {segment.estimated for segment in self.segments}
        if len(estimated) > 1:
            raise TxtFormatError(
                f"{self.source_file}: some timeline clocks carry `~` (estimated) and some do not; "
                "a file must be one or the other"
            )


def parse_txt(text: str, source_file: str) -> TxtDocument:
    """Parse a transcript .txt into a `TxtDocument`, or raise `TxtFormatError`."""
    return _Parser(text, source_file).parse()
