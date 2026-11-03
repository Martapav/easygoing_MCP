"""Ingest tests on small hand-made fixtures (section 9).

Covers every case section 9 lists: both timecode forms, guest-vs-rubric chapter
classification, the `[da verificare]` annotation, apostrophe normalization,
registry merge preserving curated fields, and unknown formats failing.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from easygoing_mcp.config import load_settings
from easygoing_mcp.ingest import mb_mymeeting_en_v0 as en_v0
from easygoing_mcp.ingest import mb_mymeeting_it_v1 as it_v1
from easygoing_mcp.ingest.detect import UnknownSourceFormat, detect_format
from easygoing_mcp.ingest.slugs import normalize, slugify
from easygoing_mcp.ingest.timecodes import (
    TimecodeError,
    format_timecode,
    parse_timecode,
    parse_timecode_annotated,
)
from easygoing_mcp.models.source_en_v0 import EnV0Document
from easygoing_mcp.models.source_it_v1 import ItV1Document

# The en_v0 adapter cross-checks the date against the file name, so the fixtures
# are adapted under a name shaped like a real delivery.
EN_V0_SOURCE_FILE = "Market_Briefing_2026-10-28_FIXTURE.json"
IT_V1_SOURCE_FILE = "Market_Briefing_2026-10-29_FIXTURE.json"


class TestTimecodes:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("00:00", 0),
            ("10:40", 640),
            ("59:59", 3599),
            ("1:00:00", 3600),
            ("1:39:01", 5941),   # MB20261028 content end
            ("01:39:01", 5941),  # zero-padded hours
        ],
    )
    def test_parses_both_forms(self, value, expected):
        """`mm:ss` below one hour, `h:mm:ss` above; both occur in MB20261028."""
        assert parse_timecode(value) == expected

    @pytest.mark.parametrize("value", ["1:75", "1:2:3:4", "abc", "", "  ", "12", "1:aa"])
    def test_rejects_malformed(self, value):
        """A malformed timecode fails rather than being coerced into a plausible number."""
        with pytest.raises(TimecodeError):
            parse_timecode(value)

    def test_strips_annotation(self):
        assert parse_timecode_annotated("25:02 [da verificare]") == (1502, "da verificare")

    def test_no_annotation_returns_none(self):
        assert parse_timecode_annotated("11:47") == (707, None)

    def test_format_round_trip(self):
        assert format_timecode(5941) == "01:39:01"
        assert parse_timecode(format_timecode(5941)) == 5941


class TestSlugs:
    def test_apostrophe_forms_converge(self):
        """The real data only ever uses the curly form, so this is the one rule
        the live files cannot exercise — hence a fixture-backed test."""
        assert slugify("Chiediamolo all’AI") == slugify("Chiediamolo all'AI") == "chiediamolo-all-ai"

    def test_accents_are_stripped(self):
        assert slugify("La Voce dell’Agorà") == "la-voce-dell-agora"

    def test_matches_the_documented_examples(self):
        assert slugify("Davide Biocchi") == "davide-biocchi"
        assert slugify("Grafico del giorno") == "grafico-del-giorno"

    def test_normalize_keeps_the_apostrophe_but_unifies_it(self):
        assert normalize("Chiediamolo all’AI") == "chiediamolo all'ai"

    def test_empty_slug_is_an_error(self):
        with pytest.raises(ValueError, match="no alphanumeric content"):
            slugify("---")


class TestDetection:
    def test_detects_en_v0(self, en_v0_raw):
        assert detect_format(en_v0_raw) == "mb_mymeeting_en_v0"

    def test_detects_it_v1(self, it_v1_raw):
        assert detect_format(it_v1_raw) == "mb_mymeeting_it_v1"

    @pytest.mark.parametrize("document", [{}, {"segments": []}, {"schema": "something.else.v9"}, []])
    def test_unknown_format_fails(self, document):
        """No best-effort parsing: a wrong adapter would silently distort timings."""
        with pytest.raises(UnknownSourceFormat):
            detect_format(document)

    def test_error_names_the_known_formats(self):
        with pytest.raises(UnknownSourceFormat, match="mb_mymeeting_en_v0"):
            detect_format({"unexpected": True})


class TestStrictSourceModels:
    def test_unknown_field_is_rejected(self, en_v0_raw):
        with pytest.raises(ValidationError):
            EnV0Document.model_validate({**en_v0_raw, "extra_key": 1})

    def test_missing_field_is_rejected(self, it_v1_raw):
        payload = {k: v for k, v in it_v1_raw.items() if k != "titolo"}
        with pytest.raises(ValidationError):
            ItV1Document.model_validate(payload)


class TestEnV0Adapter:
    @pytest.fixture
    def adapted(self, en_v0_raw):
        canonical, _ = en_v0.adapt(EnV0Document.model_validate(en_v0_raw), EN_V0_SOURCE_FILE)
        return canonical

    def test_live_fields(self, adapted):
        assert adapted.live.live_id == "MBFIX"
        assert adapted.live.date == "2026-10-28"
        # The source has no title; the programme name is not a title (section 4.2).
        assert adapted.live.title is None
        assert adapted.live.programme_id == "market-briefing"
        assert adapted.coverage.content_end_s == 3630
        assert adapted.coverage.mention_index_scope == "equities_only"

    def test_guest_chapter(self, adapted):
        chapter = adapted.chapters[0]
        assert chapter.speaker_ids == ["ada-lovelace"]
        assert chapter.rubric_id is None
        assert chapter.topics_raw == "Fed, rates"

    def test_rubric_chapter_with_topics(self, adapted):
        chapter = adapted.chapters[1]
        assert chapter.rubric_id == "grafico-del-giorno"
        assert chapter.speaker_ids == []
        assert chapter.topics_raw == "Snam"

    def test_bare_name_rubric_has_no_topics(self, adapted):
        """`Spazio alle grida` carries no `— Right` part."""
        chapter = adapted.chapters[2]
        assert chapter.rubric_id == "spazio-alle-grida"
        assert chapter.topics_raw is None

    def test_chapters_assigned_by_time(self, adapted):
        assert [s.chapter_id for s in adapted.segments] == [
            "MBFIX-C01", "MBFIX-C02", "MBFIX-C02", "MBFIX-C03"
        ]

    def test_start_quality_mapping(self, adapted):
        assert [s.start_quality for s in adapted.segments] == [
            "asr_chunk_start", "chapter_official", "asr_chunk_start", "chapter_official"
        ]

    def test_zero_duration_anomaly(self, adapted):
        zero = [a for a in adapted.anomalies if a.kind == "zero_duration"]
        assert [a.segment_ids for a in zero] == [["MBFIX-002"]]

    def test_needs_audio_review_follows_confidence(self, adapted):
        flagged = [s.segment_id for s in adapted.segments if s.speaker.needs_audio_review]
        assert flagged == ["MBFIX-002"]
        # The source vocabulary is kept for display, not remapped (section 4.1).
        assert adapted.segments[1].speaker.confidence == "review_audio"

    def test_unmapped_source_fields_are_preserved(self, adapted):
        assert adapted.segments[0].source["source_line"] == 1
        assert "disclaimer" in adapted.live.source_meta

    def test_date_mismatch_fails(self, en_v0_raw):
        with pytest.raises(en_v0.AdapterError, match="date mismatch"):
            en_v0.adapt(EnV0Document.model_validate(en_v0_raw), "Market_Briefing_2026-10-27_X.json")

    def test_unknown_timecode_kind_fails(self, en_v0_raw):
        payload = json.loads(json.dumps(en_v0_raw))
        payload["segments"][0]["timecode_kind"] = "brand_new_kind"
        with pytest.raises(en_v0.AdapterError, match="unknown timecode_kind"):
            en_v0.adapt(EnV0Document.model_validate(payload), EN_V0_SOURCE_FILE)


class TestItV1Adapter:
    @pytest.fixture
    def adapted(self, it_v1_raw):
        canonical, notes = it_v1.adapt(ItV1Document.model_validate(it_v1_raw), IT_V1_SOURCE_FILE)
        return canonical

    def test_live_fields(self, adapted):
        assert adapted.live.live_id == "MB20261029"
        assert adapted.live.title == "FIXTURE LIVE"
        # No video URL, so no deep links anywhere downstream (section 4.6).
        assert adapted.live.video_url is None
        assert adapted.live.duration_seconds is None
        assert adapted.coverage.truncated is True
        assert adapted.coverage.mention_index_scope == "multi_asset"

    def test_segment_ids_are_zero_padded(self, adapted):
        assert [s.segment_id for s in adapted.segments][:2] == ["MB20261029-000", "MB20261029-001"]

    def test_annotated_chapter_is_marked_unverified(self, adapted):
        chapter = adapted.chapters[1]
        assert chapter.start_s == 1502
        assert chapter.start_quality == "chapter_official_unverified"

    def test_unannotated_chapter_is_official(self, adapted):
        assert adapted.chapters[0].start_quality == "chapter_official"

    def test_guest_chapter_from_speaker_ufficiali(self, adapted):
        assert adapted.chapters[2].speaker_ids == ["ada-lovelace"]
        assert adapted.chapters[2].rubric_id is None

    def test_apostrophe_name_slugs_to_the_shared_id(self, adapted):
        """The fixture writes the straight-quote form; the real file uses the curly
        one. Both must land on the same rubric id."""
        assert adapted.chapters[1].rubric_id == "chiediamolo-all-ai"

    def test_chapters_assigned_by_rubrica_not_time(self, adapted):
        # Segment 001 sits at 1502 s, which by time falls in C02, but its rubrica
        # says Apertura e mercati, so it stays in C01.
        assert adapted.segments[1].chapter_id == "MB20261029-C01"

    def test_conflict_is_recorded(self, adapted):
        conflicts = [a for a in adapted.anomalies if a.kind == "chapter_assignment_conflict"]
        assert [a.segment_ids for a in conflicts] == [["MB20261029-001"]]
        assert "rubrica" in conflicts[0].detail

    def test_final_segment_is_not_a_zero_duration_anomaly(self, adapted):
        """Its end is unknown because the live is truncated, which coverage reports."""
        zero = [a for a in adapted.anomalies if a.kind == "zero_duration"]
        assert [a.segment_ids for a in zero] == [["MB20261029-001"]]
        assert adapted.segments[-1].start_s == adapted.segments[-1].end_s

    def test_limiti_become_anomalies(self, adapted):
        limitations = [a for a in adapted.anomalies if a.kind == "source_limitation"]
        assert len(limitations) == 2
        assert "solo fonte TXT" in limitations[0].detail

    def test_time_based_assignment_is_a_one_line_switch(self, it_v1_raw):
        settings = load_settings({"EASYGOING_CHAPTER_ASSIGNMENT_IT_V1": "time"})
        canonical, _ = it_v1.adapt(
            ItV1Document.model_validate(it_v1_raw), IT_V1_SOURCE_FILE, settings
        )
        # The same segment now follows its interpolated time into C02.
        assert canonical.segments[1].chapter_id == "MB20261029-C02"

    def test_unknown_rubrica_fails(self, it_v1_raw):
        payload = json.loads(json.dumps(it_v1_raw))
        payload["segmenti"][0]["rubrica"] = "Rubrica inesistente"
        with pytest.raises(it_v1.AdapterError, match="matches no chapter name"):
            it_v1.adapt(ItV1Document.model_validate(payload), IT_V1_SOURCE_FILE)

    def test_mentions_map_to_canonical_segment_ids(self, adapted):
        assert {(m.instrument_id, m.segment_id) for m in adapted.mentions} == {
            ("alphabet-google", "MB20261029-000"),
            ("oro", "MB20261029-002"),
        }
