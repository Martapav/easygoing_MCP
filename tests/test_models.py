"""Unit tests for the canonical models (section 9).

Covers the states the real data puts us in — zero-duration segments, a live
with no video URL, a truncated live, `mixed_speakers` null — and the states
that must be rejected rather than tolerated.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from easygoing_mcp.models.canonical import (
    CanonicalLive,
    Chapter,
    Coverage,
    Live,
    Mention,
    Segment,
    SpeakerAttribution,
)


def make_segment(segment_id: str = "L-001", start_s: int = 0, end_s: int = 10, **overrides) -> Segment:
    payload = {
        "segment_id": segment_id,
        "start_s": start_s,
        "end_s": end_s,
        "start_quality": "asr_chunk_start",
        "chapter_id": None,
        "text": "testo",
        "speaker": SpeakerAttribution(speaker_id="ada-lovelace", confidence="alta", needs_audio_review=False),
        "mixed_speakers": None,
        "source": {},
    }
    payload.update(overrides)
    return Segment(**payload)


def make_live(**overrides) -> Live:
    payload = {
        "live_id": "L", "date": "2026-10-29", "title": None, "programme_id": "market-briefing",
        "video_url": None, "source_format": "mb_mymeeting_it_v1", "source_file": "f.json",
        "duration_seconds": None, "ingest_schema_version": "1.0",
    }
    payload.update(overrides)
    return Live(**payload)


class TestSegment:
    def test_zero_duration_is_valid(self):
        """25 segments in MB20261028 and 31 in MB20261029 are zero-length by
        construction; the model must accept them rather than treat them as bad data."""
        segment = make_segment(start_s=640, end_s=640)
        assert segment.start_s == segment.end_s == 640

    def test_end_before_start_is_rejected(self):
        with pytest.raises(ValidationError, match="precedes start_s"):
            make_segment(start_s=100, end_s=50)

    def test_unknown_start_quality_is_rejected(self):
        with pytest.raises(ValidationError):
            make_segment(start_quality="something_new")

    def test_mixed_speakers_stays_null(self):
        """Neither source marks it per segment, and it is never inferred (section 4.1)."""
        assert make_segment().mixed_speakers is None

    def test_mixed_speakers_accepts_a_boolean_if_a_source_ever_marks_it(self):
        assert make_segment(mixed_speakers=True).mixed_speakers is True

    def test_unknown_field_is_rejected(self):
        with pytest.raises(ValidationError):
            make_segment(speaker_name="Ada Lovelace")


class TestLiveAndCoverage:
    def test_live_without_video_url_is_valid(self):
        """MB20261029 has no video URL, so deep links cannot be built (section 4.6)."""
        assert make_live().video_url is None

    def test_truncated_coverage(self):
        coverage = Coverage(
            content_end_s=5481, truncated=True,
            mention_index_scope="multi_asset", timestamps_kind="estimated_interpolated",
        )
        assert coverage.truncated is True

    def test_unknown_mention_index_scope_is_rejected(self):
        with pytest.raises(ValidationError):
            Coverage(
                content_end_s=1, truncated=False,
                mention_index_scope="everything", timestamps_kind="asr_chunk_start",
            )

    def test_unknown_source_format_is_rejected(self):
        with pytest.raises(ValidationError):
            make_live(source_format="mb_mymeeting_fr_v2")


class TestChapter:
    def test_guest_block_has_speakers_and_no_rubric(self):
        chapter = Chapter(
            chapter_id="L-C01", start_s=0, start_quality="chapter_official",
            label_raw="Ada Lovelace — Fed", rubric_id=None,
            speaker_ids=["ada-lovelace"], topics_raw="Fed",
        )
        assert chapter.rubric_id is None and chapter.speaker_ids == ["ada-lovelace"]

    def test_rubric_and_speakers_cannot_coexist(self):
        with pytest.raises(ValidationError, match="one or the other"):
            Chapter(
                chapter_id="L-C01", start_s=0, start_quality="chapter_official",
                label_raw="x", rubric_id="grafico-del-giorno",
                speaker_ids=["ada-lovelace"], topics_raw=None,
            )


class TestCanonicalLive:
    def _build(self, **overrides):
        payload = {
            "live": make_live(),
            "coverage": Coverage(
                content_end_s=100, truncated=False,
                mention_index_scope="multi_asset", timestamps_kind="estimated_interpolated",
            ),
            "chapters": [
                Chapter(chapter_id="L-C01", start_s=0, start_quality="chapter_official",
                        label_raw="Apertura", rubric_id="apertura", speaker_ids=[], topics_raw=None)
            ],
            "segments": [make_segment("L-001", 0, 50, chapter_id="L-C01"),
                         make_segment("L-002", 50, 100, chapter_id="L-C01")],
            "mentions": [Mention(instrument_id="oro", segment_id="L-001", origin="source_index")],
            "anomalies": [],
        }
        payload.update(overrides)
        return CanonicalLive(**payload)

    def test_valid_document(self):
        assert len(self._build().segments) == 2

    def test_segment_referencing_unknown_chapter_is_rejected(self):
        with pytest.raises(ValidationError, match="unknown chapter"):
            self._build(segments=[make_segment("L-001", 0, 50, chapter_id="L-C99")])

    def test_mention_referencing_unknown_segment_is_rejected(self):
        with pytest.raises(ValidationError, match="unknown segment"):
            self._build(mentions=[Mention(instrument_id="oro", segment_id="L-999", origin="source_index")])

    def test_segments_out_of_order_are_rejected(self):
        with pytest.raises(ValidationError, match="not ordered by start_s"):
            self._build(segments=[make_segment("L-001", 50, 100, chapter_id="L-C01"),
                                  make_segment("L-002", 0, 50, chapter_id="L-C01")])

    def test_duplicate_segment_ids_are_rejected(self):
        with pytest.raises(ValidationError, match="duplicate segment_id"):
            self._build(segments=[make_segment("L-001", 0, 50, chapter_id="L-C01"),
                                  make_segment("L-001", 50, 100, chapter_id="L-C01")])
