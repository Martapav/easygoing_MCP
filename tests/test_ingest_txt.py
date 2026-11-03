"""The standard .txt ingest route (`transcript_txt_v1`, docs/transcript_txt_format.md).

Three layers:
- parser and detection on small hand-written transcripts, covering both
  documented variants and every way a line can be rejected;
- metadata resolution, including the temporary companion-JSON fallback;
- parity against the JSON route on the two real lives, whose segment text is
  identical in both sources, so the JSON route is an exact oracle.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from easygoing_mcp.config import load_settings
from easygoing_mcp.ingest import transcript_txt_v1 as txt_v1
from easygoing_mcp.ingest.detect import UnknownSourceFormat, detect_source
from easygoing_mcp.ingest.txt_metadata import MetadataError, resolve_metadata
from easygoing_mcp.ingest.txt_parse import TxtFormatError, parse_txt
from easygoing_mcp.models.canonical import CanonicalLive

REAL_DATA = Path(__file__).resolve().parents[1] / "data"
TXT_28 = REAL_DATA / "source" / "Market_Briefing_2026-10-28_TRASCRIZIONE_CORRETTA.txt"
TXT_29 = REAL_DATA / "source" / "Market_Briefing_2026-10-29_TRASCRIZIONE_CORRETTA.txt"

NAME = "Market_Briefing_2026-11-05_TEST.txt"

META = """---
title: Una prova
video_url: https://youtube.com/live/abc
content_end: 1:01:00
truncated: false
mention_index_scope: multi_asset
timestamps: asr_chunk_start
guests: Anna Rossi
---
"""

# Variant A: bare clocks, column chapter list, first-occurrence index, notes.
VARIANT_A = META + """MARKET BRIEFING — 5 NOVEMBRE 2026
Testo libero dell'intestazione.
Video: https://youtube.com/live/abc

CAPITOLI UFFICIALI YOUTUBE
  00:00  Anna Rossi — Mercati, Stellantis
  05:00  Grafico del giorno — L'Oréal
  1:00:00  Chiediamolo all’AI

TITOLI E NOMI RICONOSCIUTI
  Stellantis (STLAM) — prima occorrenza 00:00
  L'Oréal (OR) — prima occorrenza 05:00
  SK hynix (000660 (KRX)) — prima occorrenza 05:10
  Oro — prima occorrenza 05:00

TRASCRIZIONE
=========================

[00:00] Anna Rossi: Buongiorno, parliamo di Stellantis.
[00:10] Anna Rossi [speaker da verificare]: Il lavoro continua, or ora.
[05:00] Anna Rossi: Il grafico: L'Oréal, ticker OR, e l'oro.
[05:00] Marco Bianchi: SK hynix sale.
[1:00:05] Marco Bianchi [?]: Chiudiamo.

NOTE DI QUALITÀ
- Numeri non verificati.
- Attribuzioni da contesto.
"""

# Variant B: labelled clocks with an estimated YT clock, pipe chapters, section headers.
VARIANT_B = """MARKET BRIEFING — 5 NOVEMBRE 2026
OSPITI UFFICIALI: Anna Rossi, Marco Bianchi.

CAPITOLI UFFICIALI YOUTUBE (forniti)
     00:00 | Apertura — mercati
     02:00 | Anna Rossi — tassi
04:00 [da verificare] | Spazio alle grida
     06:00 | Anna Rossi — seconda parte

TRASCRIZIONE (REC / YT~)

========== APERTURA ==========
[REC 00:01:00 | YT~ 00:00:00] Marco Bianchi: Apriamo.
[REC 00:02:00 | YT~ 00:01:00] Marco Bianchi [?]: L'euro sale.
========== ANNA ROSSI ==========
[REC 00:03:30 | YT~ 00:02:30] Anna Rossi: Tassi.
========== SPAZIO ALLE GRIDA ==========
[REC 00:04:30 | YT~ 00:03:30] Marco Bianchi: Grida, in anticipo sul capitolo.
========== ANNA ROSSI ==========
[REC 00:07:00 | YT~ 00:06:00] Anna Rossi: Di nuovo io.

INDICE DI NOMI/TITOLI
Euro/Dollaro (EURUSD) | 1 segmenti | Marco Bianchi
Stellantis (STLAM) | 2 segmenti | Anna Rossi
"""


def parse(text: str, name: str = NAME):
    return parse_txt(text, name)


def write(tmp_path: Path, text: str, name: str = NAME) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def build(text: str, name: str = NAME, *, source_dir: Path | None = None, aliases=None, settings=None):
    document = parse(text, name)
    meta = resolve_metadata(document, source_dir, settings)
    return txt_v1.adapt(document, meta, settings, aliases)


# --- parser: variant A ------------------------------------------------------------

class TestVariantA:
    def test_header_blocks(self):
        doc = parse(VARIANT_A)
        assert doc.date == "2026-11-05"
        assert doc.programme_name == "MARKET BRIEFING"
        assert doc.video_url == "https://youtube.com/live/abc"
        assert doc.header_text == ["Testo libero dell'intestazione.", "TRASCRIZIONE"]
        assert doc.metadata.guests == ["Anna Rossi"]
        assert doc.metadata.truncated is False
        assert doc.notes == ["Numeri non verificati.", "Attribuzioni da contesto."]

    def test_mm_ss_and_h_mm_ss_clocks(self):
        doc = parse(VARIANT_A)
        assert [s.start_s for s in doc.segments] == [0, 10, 300, 300, 3605]
        assert [c.start_s for c in doc.chapters] == [0, 300, 3600]

    def test_review_flags(self):
        doc = parse(VARIANT_A)
        assert [s.review_flag for s in doc.segments] == [None, "speaker da verificare", None, None, "?"]

    def test_chapter_label_split(self):
        doc = parse(VARIANT_A)
        assert (doc.chapters[0].name, doc.chapters[0].topics) == ("Anna Rossi", "Mercati, Stellantis")
        assert (doc.chapters[2].name, doc.chapters[2].topics) == ("Chiediamolo all’AI", None)

    def test_index_tickers_keep_nested_parentheses(self):
        doc = parse(VARIANT_A)
        assert doc.index_form == "first_mention"
        by_name = {e.name: e.tickers_raw for e in doc.index}
        assert by_name == {"Stellantis": "STLAM", "L'Oréal": "OR", "SK hynix": "000660 (KRX)", "Oro": None}


# --- parser: variant B ------------------------------------------------------------

class TestVariantB:
    def test_labelled_clocks(self):
        doc = parse(VARIANT_B)
        first = doc.segments[0]
        assert (first.start_s, first.estimated, first.other_clocks) == (0, True, {"REC": "00:01:00"})

    def test_da_verificare_chapter_annotation(self):
        doc = parse(VARIANT_B)
        assert doc.chapters[2].annotation == "da verificare"
        assert doc.chapters[2].start_s == 240

    def test_guests_line_and_index_counts(self):
        doc = parse(VARIANT_B)
        assert doc.guests == ["Anna Rossi", "Marco Bianchi"]
        assert doc.index_form == "segment_count"
        assert doc.index[0].stated_segment_count == 1
        assert doc.index[0].stated_speakers == ["Marco Bianchi"]

    def test_section_occurrences(self):
        doc = parse(VARIANT_B)
        assert [(s.section, s.section_occurrence) for s in doc.segments] == [
            ("APERTURA", 1), ("APERTURA", 1), ("ANNA ROSSI", 1), ("SPAZIO ALLE GRIDA", 1), ("ANNA ROSSI", 2),
        ]


# --- parser: rejections -----------------------------------------------------------

class TestRejections:
    def test_unknown_line_inside_body_names_its_line(self):
        text = VARIANT_A.replace("[05:00] Marco Bianchi:", "Marco Bianchi dice:")
        with pytest.raises(TxtFormatError, match=rf"{NAME}:\d+: inside the transcript"):
            parse(text)

    def test_prose_after_the_transcript_is_rejected(self):
        with pytest.raises(TxtFormatError, match="unexpected line after the transcript"):
            parse(VARIANT_A + "\nUna riga qualsiasi.\n")

    def test_bad_line_inside_chapter_block(self):
        text = VARIANT_A.replace("  05:00  Grafico del giorno — L'Oréal", "  Grafico del giorno")
        with pytest.raises(TxtFormatError, match="expected a chapter line"):
            parse(text)

    def test_unknown_metadata_key(self):
        with pytest.raises(TxtFormatError, match="unknown metadata key 'speaker'"):
            parse("---\nspeaker: x\n---\n" + VARIANT_B)

    def test_bad_truncated_value(self):
        with pytest.raises(TxtFormatError, match="truncated must be true or false"):
            parse(VARIANT_A.replace("truncated: false", "truncated: forse"))

    def test_title_date_must_match_file_name(self):
        with pytest.raises(TxtFormatError, match="disagrees with file name date"):
            parse(VARIANT_B, "Market_Briefing_2026-11-06_TEST.txt")

    def test_file_name_must_carry_programme_and_date(self):
        with pytest.raises(TxtFormatError, match="file name must be"):
            parse(VARIANT_B, "trascrizione.txt")

    def test_decreasing_segment_time(self):
        text = VARIANT_A.replace("[1:00:05] Marco Bianchi [?]", "[04:00] Marco Bianchi [?]")
        with pytest.raises(TxtFormatError, match="precedes the previous segment"):
            parse(text)

    def test_mixed_estimated_and_exact_clocks(self):
        text = VARIANT_B.replace("YT~ 00:02:30", "YT 00:02:30")
        with pytest.raises(TxtFormatError, match="one or the other"):
            parse(text)

    def test_labelled_clocks_need_a_yt_clock(self):
        text = VARIANT_B.replace("[REC 00:01:00 | YT~ 00:00:00]", "[REC 00:01:00]")
        with pytest.raises(TxtFormatError, match="labelled `YT`"):
            parse(text)

    def test_malformed_timecode(self):
        with pytest.raises(TxtFormatError, match="out-of-range"):
            parse(VARIANT_A.replace("[00:10]", "[00:75]"))


class TestDetection:
    def test_txt_is_claimed(self, tmp_path):
        assert detect_source(write(tmp_path, VARIANT_A)) == "transcript_txt_v1"

    def test_arbitrary_txt_is_unknown(self, tmp_path):
        with pytest.raises(UnknownSourceFormat, match="not a transcript_txt_v1 transcript"):
            detect_source(write(tmp_path, "appunti\nnessuna struttura\n"))

    def test_other_suffix_is_unknown(self, tmp_path):
        path = tmp_path / "x.csv"
        path.write_text("a,b\n", encoding="utf-8")
        with pytest.raises(UnknownSourceFormat, match="unsupported file type"):
            detect_source(path)


# --- metadata -----------------------------------------------------------------------

class TestMetadata:
    def test_block_alone_is_enough(self):
        doc = parse(VARIANT_A)
        meta = resolve_metadata(doc, None)
        assert (meta.content_end_s, meta.truncated, meta.mention_index_scope, meta.timestamps_kind) == (
            3660, False, "multi_asset", "asr_chunk_start",
        )
        assert meta.fallback == {}
        assert meta.origins["video_url"] == "metadata_block"

    def test_tilde_states_estimated_timestamps(self):
        text = "---\ntruncated: true\nmention_index_scope: multi_asset\n---\n" + VARIANT_B
        meta = resolve_metadata(parse(text), None)
        assert meta.timestamps_kind == "estimated_interpolated"
        assert meta.origins["timestamps_kind"] == "clock_tilde"
        # Truncated and no content_end: the last segment's start is the end.
        assert (meta.content_end_s, meta.origins["content_end_s"]) == (360, "last_segment_start")

    def test_missing_required_field_without_companion_fails(self):
        with pytest.raises(MetadataError, match="no companion JSON"):
            resolve_metadata(parse(VARIANT_B), None)

    def test_not_truncated_without_content_end_fails(self):
        text = "---\ntruncated: false\nmention_index_scope: multi_asset\n---\n" + VARIANT_B
        with pytest.raises(MetadataError, match="cannot be derived"):
            resolve_metadata(parse(text), None)

    def test_block_contradicting_the_clocks_fails(self):
        text = "---\ntimestamps: asr_chunk_start\n---\n" + VARIANT_B
        with pytest.raises(MetadataError, match="carry `~`"):
            resolve_metadata(parse(text), None)

    def test_companion_fallback_is_recorded(self):
        doc = parse(TXT_28.read_text(encoding="utf-8"), TXT_28.name)
        meta = resolve_metadata(doc, REAL_DATA / "source")
        assert meta.content_end_s == 5941
        assert set(meta.fallback) == {"title", "content_end_s", "truncated", "mention_index_scope", "timestamps_kind"}
        assert meta.origins["video_url"] == "video_line"
        assert any("TEMPORARY fallback" in note for note in meta.notes)

    def test_txt_wins_over_companion_and_warns(self, tmp_path):
        companion = REAL_DATA / "source" / "Market_Briefing_2026-10-28_MCP_TEST.json"
        shutil.copy(companion, tmp_path / companion.name)
        text = "---\ncontent_end: 1:39:00\n---\n" + TXT_28.read_text(encoding="utf-8")
        doc = parse(text, TXT_28.name)
        meta = resolve_metadata(doc, tmp_path)
        assert meta.content_end_s == 5940
        assert "content_end_s" not in meta.fallback
        assert any("kept the .txt value" in w for w in meta.warnings)


# --- adapter ----------------------------------------------------------------------

class TestAdapter:
    def test_ids_and_live_fields(self):
        live = build(VARIANT_A).canonical
        assert live.live.live_id == "MB20261105"
        assert live.live.programme_id == "market-briefing"
        assert [s.segment_id for s in live.segments][:2] == ["MB20261105-001", "MB20261105-002"]
        assert live.live.source_format == "transcript_txt_v1"
        assert live.live.duration_seconds == 3660
        assert live.live.source_meta["header_text"] == ["Testo libero dell'intestazione.", "TRASCRIZIONE"]

    def test_guest_or_rubric(self):
        chapters = build(VARIANT_A).canonical.chapters
        assert (chapters[0].speaker_ids, chapters[0].rubric_id) == (["anna-rossi"], None)
        assert (chapters[1].speaker_ids, chapters[1].rubric_id) == ([], "grafico-del-giorno")
        assert chapters[2].rubric_id == "chiediamolo-all-ai"  # typographic apostrophe normalized

    def test_review_flag_drives_audio_review(self):
        segments = build(VARIANT_A).canonical.segments
        assert [s.speaker.needs_audio_review for s in segments] == [False, True, False, False, True]
        assert segments[0].speaker.confidence == "unflagged"
        assert segments[1].speaker.confidence == "speaker da verificare"
        assert all(s.mixed_speakers is None for s in segments)

    def test_zero_duration_and_time_assignment(self):
        live = build(VARIANT_A).canonical
        zero = [a.segment_ids for a in live.anomalies if a.kind == "zero_duration"]
        assert zero == [["MB20261105-003"]]
        assert [s.chapter_id[-3:] for s in live.segments] == ["C01", "C01", "C02", "C02", "C03"]

    def test_notes_become_source_limitations(self):
        live = build(VARIANT_A).canonical
        assert [a.detail for a in live.anomalies if a.kind == "source_limitation"] == [
            "Numeri non verificati.", "Attribuzioni da contesto.",
        ]

    def test_section_headers_assign_and_conflicts_are_recorded(self):
        text = "---\ntruncated: true\nmention_index_scope: multi_asset\n---\n" + VARIANT_B
        live = build(text).canonical
        # The 4th segment sits under SPAZIO ALLE GRIDA (C03, 240 s) but starts at 210 s (C02).
        assert [s.chapter_id[-3:] for s in live.segments] == ["C01", "C01", "C02", "C03", "C04"]
        conflicts = [a for a in live.anomalies if a.kind == "chapter_assignment_conflict"]
        assert [a.segment_ids for a in conflicts] == [["MB20261105-004"]]
        assert live.chapters[2].start_quality == "chapter_official_unverified"

    def test_time_assignment_switch(self):
        text = "---\ntruncated: true\nmention_index_scope: multi_asset\n---\n" + VARIANT_B
        settings = load_settings({"EASYGOING_CHAPTER_ASSIGNMENT_TXT_V1": "time"})
        live = build(text, settings=settings).canonical
        assert live.segments[3].chapter_id.endswith("C02")

    def test_segment_before_first_header_fails(self):
        text = "---\ntruncated: true\nmention_index_scope: multi_asset\n---\n" + VARIANT_B.replace(
            "========== APERTURA ==========\n", ""
        )
        with pytest.raises(txt_v1.AdapterError, match="precedes the first section header"):
            build(text)

    def test_unknown_section_header_fails(self):
        text = "---\ntruncated: true\nmention_index_scope: multi_asset\n---\n" + VARIANT_B.replace(
            "== APERTURA ==", "== CHIUSURA =="
        )
        with pytest.raises(txt_v1.AdapterError, match="matches no chapter"):
            build(text)

    def test_content_end_before_last_segment_fails(self):
        with pytest.raises(txt_v1.AdapterError, match="content_end_s 600 s .* precedes"):
            build(VARIANT_A.replace("content_end: 1:01:00", "content_end: 10:00"))


class TestMentions:
    def mentions(self, result) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for m in result.canonical.mentions:
            out.setdefault(m.instrument_id, []).append(m.segment_id[-3:])
        return out

    def test_name_ticker_and_boundaries(self):
        found = self.mentions(build(VARIANT_A))
        assert found["stellantis"] == ["001"]
        # `OR` matches the ticker only, never the lowercase word `or` in segment 002.
        assert found["l-oreal"] == ["003"]
        # `Oro` matches `oro`, not `lavoro`.
        assert found["oro"] == ["003"]
        assert found["sk-hynix"] == ["004"]

    def test_origin_is_index_text_match(self):
        assert {m.origin for m in build(VARIANT_A).canonical.mentions} == {"index_text_match"}

    def test_cross_check_mismatches_are_warned(self):
        result = build(VARIANT_A)
        # SK hynix stated at 05:10, found at 05:00.
        assert any("'SK hynix': first occurrence stated 05:10" in w for w in result.warnings)

    def test_unspaced_slash_is_not_split(self):
        text = "---\ntruncated: true\nmention_index_scope: multi_asset\n---\n" + VARIANT_B
        result = build(text)
        found = self.mentions(result)
        assert "euro-dollaro" not in found  # `euro` alone is not Euro/Dollaro
        assert any("'Euro/Dollaro': no segment contains" in w for w in result.warnings)
        assert any("'Stellantis': no segment contains" in w for w in result.warnings)

    def test_registry_alias_widens_matching(self):
        text = "---\ntruncated: true\nmention_index_scope: multi_asset\n---\n" + VARIANT_B
        found = self.mentions(build(text, aliases={"euro-dollaro": ["euro"]}))
        assert found["euro-dollaro"] == ["002"]


# --- parity with the JSON route on the real lives -----------------------------------

needs_real = pytest.mark.skipif(
    not (TXT_28.exists() and TXT_29.exists() and (REAL_DATA / "canonical" / "MB20261028.canonical.json").exists()),
    reason="real sources or JSON-route canonical files missing",
)

EXPECTED = {
    "MB20261028": {"segments": 137, "zero_duration": 25, "audio_review": 72, "chapter_conflicts": 0},
    "MB20261029": {"segments": 241, "zero_duration": 31, "audio_review": 52, "chapter_conflicts": 19},
}


@needs_real
@pytest.mark.parametrize("path", [TXT_28, TXT_29], ids=["MB20261028", "MB20261029"])
class TestParity:
    @pytest.fixture
    def pair(self, path) -> tuple[CanonicalLive, CanonicalLive]:
        settings = load_settings({"EASYGOING_DATA_DIR": str(REAL_DATA)})
        doc = parse_txt(path.read_text(encoding="utf-8"), path.name)
        ours = txt_v1.adapt(doc, resolve_metadata(doc, settings.source_dir, settings), settings).canonical
        theirs = CanonicalLive.model_validate(
            json.loads((REAL_DATA / "canonical" / f"{ours.live.live_id}.canonical.json").read_text(encoding="utf-8"))
        )
        return ours, theirs

    def test_section_4_5_counts(self, pair):
        ours, _ = pair
        expected = EXPECTED[ours.live.live_id]
        assert len(ours.segments) == expected["segments"]
        assert sum(a.kind == "zero_duration" for a in ours.anomalies) == expected["zero_duration"]
        assert sum(s.speaker.needs_audio_review for s in ours.segments) == expected["audio_review"]
        assert sum(a.kind == "chapter_assignment_conflict" for a in ours.anomalies) == expected["chapter_conflicts"]

    def test_segments_match_the_json_route(self, pair):
        ours, theirs = pair
        for a, b in zip(ours.segments, theirs.segments, strict=True):
            assert (a.text, a.start_s, a.end_s, a.chapter_id) == (b.text, b.start_s, b.end_s, b.chapter_id)
            assert a.speaker.speaker_id == b.speaker.speaker_id
            assert a.speaker.needs_audio_review == b.speaker.needs_audio_review

    def test_coverage_and_chapters_match(self, pair):
        ours, theirs = pair
        assert ours.coverage == theirs.coverage
        assert ours.live.date == theirs.live.date
        assert ours.live.title == theirs.live.title
        assert ours.live.video_url == theirs.live.video_url
        for a, b in zip(ours.chapters, theirs.chapters, strict=True):
            assert (a.chapter_id, a.start_s, a.start_quality, a.rubric_id, a.speaker_ids) == (
                b.chapter_id, b.start_s, b.start_quality, b.rubric_id, b.speaker_ids,
            )

    def test_text_match_adds_no_mention_the_source_index_lacks(self, pair):
        # Precision against the JSON route's source_index: every text-matched
        # mention is one the index lists. Recall is below 100% (short spoken
        # forms such as "Samsung"); scripts/diff_canonical.py reports it.
        ours, theirs = pair
        position_ours = {s.segment_id: i for i, s in enumerate(ours.segments)}
        position_theirs = {s.segment_id: i for i, s in enumerate(theirs.segments)}
        found = {(m.instrument_id, position_ours[m.segment_id]) for m in ours.mentions}
        indexed = {(m.instrument_id, position_theirs[m.segment_id]) for m in theirs.mentions}
        assert found <= indexed
        assert len(found) / len(indexed) >= 0.8


# --- config switch and the ingest script ---------------------------------------------

class TestConfigAndScript:
    def test_canonical_source_switch(self, tmp_path):
        assert load_settings({"EASYGOING_DATA_DIR": str(tmp_path)}).canonical_dir == tmp_path / "canonical"
        txt = load_settings({"EASYGOING_DATA_DIR": str(tmp_path), "EASYGOING_CANONICAL_SOURCE": "txt"})
        assert txt.canonical_dir == tmp_path / "canonical_txt"

    def test_bad_canonical_source(self):
        with pytest.raises(ValueError, match="EASYGOING_CANONICAL_SOURCE"):
            load_settings({"EASYGOING_CANONICAL_SOURCE": "csv"})

    @needs_real
    def test_txt_route_writes_only_canonical_txt(self, tmp_path, capsys):
        import ingest  # scripts/ is on sys.path (conftest)

        data = tmp_path / "data"
        shutil.copytree(REAL_DATA / "source", data / "source")
        shutil.copytree(REAL_DATA / "registries", data / "registries")
        before = {p.name: p.read_bytes() for p in (data / "registries").iterdir()}

        assert ingest.main(["--data-dir", str(data), "--route", "txt"]) == 0
        out = capsys.readouterr().out
        assert "route txt" in out
        assert sorted(p.name for p in (data / "canonical_txt").iterdir()) == [
            "MB20261028.canonical.json", "MB20261029.canonical.json",
        ]
        assert not (data / "canonical").exists()
        # Nothing new for the registries: the merge rewrote them byte-identically.
        assert {p.name: p.read_bytes() for p in (data / "registries").iterdir()} == before
