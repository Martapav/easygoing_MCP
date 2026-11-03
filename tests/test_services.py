"""Service-level tests for the tools (milestone 3).

These call the service functions directly, without MCP, which is the point of
keeping `services/` free of MCP imports (section 2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from easygoing_mcp.config import Settings, load_settings
from easygoing_mcp.services.mentions import TopicRankingUnavailable, rank_subjects
from easygoing_mcp.services.rubrics import resolve_rubric, rubric_occurrences
from easygoing_mcp.services.search import (
    InvalidCursor,
    clamp_limit,
    decode_cursor,
    encode_cursor,
    find_mentions,
    get_subject_discussion,
)
from easygoing_mcp.services.speakers import AmbiguousSpeaker, resolve_speaker, speaker_coverage
from easygoing_mcp.store.base import UnknownLive, UnknownRubric, UnknownSpeaker
from easygoing_mcp.store.json_store import JsonContentStore

REAL_DATA = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(
    not (REAL_DATA / "canonical" / "MB20261028.canonical.json").exists(),
    reason="canonical files not built; run scripts/ingest.py",
)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings({"EASYGOING_DATA_DIR": str(REAL_DATA)})


@pytest.fixture(scope="module")
def store(settings) -> JsonContentStore:
    return JsonContentStore(settings)


class TestCursors:
    def test_round_trip(self):
        assert decode_cursor(encode_cursor(25)) == 25

    def test_absent_cursor_is_the_first_page(self):
        assert decode_cursor(None) == 0 and decode_cursor("") == 0

    def test_cursor_is_opaque(self):
        """Not a bare number, so the paging scheme stays changeable."""
        assert encode_cursor(10) != "10"

    @pytest.mark.parametrize("bad", ["not-a-cursor", "!!!", "eyJ4IjoxfQ"])
    def test_foreign_cursor_is_rejected_clearly(self, bad):
        with pytest.raises(InvalidCursor, match="cursor"):
            decode_cursor(bad)


class TestLimits:
    def test_default(self, settings):
        assert clamp_limit(None, settings) == 10

    def test_hard_maximum_is_enforced(self, settings):
        """Section 5: a caller cannot ask for an unbounded list."""
        assert clamp_limit(10_000, settings) == settings.max_limit == 50

    def test_minimum_is_one(self, settings):
        assert clamp_limit(0, settings) == 1
        assert clamp_limit(-5, settings) == 1


class TestFindMentions:
    def test_subject_found_in_both_lives(self, store, settings):
        """Section 9 acceptance: Stellantis hits in MB20261028 and MB20261029."""
        result = find_mentions(store, settings, "Stellantis", limit=50)
        assert {g.live_id for g in result.groups} == {"MB20261028", "MB20261029"}

    def test_section_9_evidence_windows(self, store, settings):
        result = find_mentions(store, settings, "Stellantis", limit=50)
        by_live = {g.live_id: g for g in result.groups}
        assert any(1496 <= s.start_s <= 1651 for s in by_live["MB20261028"].segments)
        assert any(2605 <= s.start_s <= 2722 for s in by_live["MB20261029"].segments)

    def test_groups_are_newest_live_first(self, store, settings):
        result = find_mentions(store, settings, "Stellantis", limit=50)
        assert [g.live_id for g in result.groups] == ["MB20261029", "MB20261028"]

    def test_segments_within_a_group_are_in_broadcast_order(self, store, settings):
        result = find_mentions(store, settings, "Stellantis", limit=50)
        for group in result.groups:
            starts = [s.start_s for s in group.segments]
            assert starts == sorted(starts)

    def test_paging_covers_everything_exactly_once(self, store, settings):
        seen: list[str] = []
        cursor = None
        while True:
            page = find_mentions(store, settings, "Stellantis", limit=5, cursor=cursor)
            seen += [s.segment_id for g in page.groups for s in g.segments]
            cursor = page.next_cursor
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 17

    def test_comparability_does_not_depend_on_the_page(self, store, settings):
        """A caveat that flipped per page would be worse than none."""
        first = find_mentions(store, settings, "Stellantis", limit=5)
        second = find_mentions(store, settings, "Stellantis", limit=5, cursor=first.next_cursor)
        assert first.comparable_across_lives is second.comparable_across_lives is False

    def test_single_live_result_is_comparable(self, store, settings):
        result = find_mentions(store, settings, "Stellantis", live_id="MB20261028")
        assert result.comparable_across_lives is True

    def test_speaker_filter_accepts_a_surname(self, store, settings):
        result = find_mentions(store, settings, "Stellantis", speaker="Di Fazio", limit=50)
        assert all(
            s.speaker_id == "fernando-di-fazio" for g in result.groups for s in g.segments
        )

    def test_date_filter(self, store, settings):
        result = find_mentions(store, settings, "Stellantis", date_to="2026-10-28", limit=50)
        assert {g.live_id for g in result.groups} == {"MB20261028"}

    def test_variants_are_reported_in_the_notes(self, store, settings):
        result = find_mentions(store, settings, "Fed")
        assert result.subject.variants_used == ["federal reserve"]
        assert any("federal reserve" in note for note in result.notes)

    def test_unknown_live_id_raises_with_options(self, store, settings):
        with pytest.raises(UnknownLive, match="MB20261029"):
            find_mentions(store, settings, "Stellantis", live_id="MB20261030")


class TestSubjectDiscussion:
    def test_fed_returns_at_least_two_speakers_in_the_evidence_windows(self, store, settings):
        """Section 9 acceptance, the case variant expansion exists for."""
        result = get_subject_discussion(store, settings, "MB20261028", "Fed")
        windows = ((2615, 2990), (3476, 3581))
        speakers = {
            group.speaker_id
            for group in result.groups
            for segment in group.segments
            if segment.segment_id in group.matched_segment_ids
            for a, b in windows
            if segment.start_s < b and segment.end_s > a
        }
        assert len(speakers) >= 2

    def test_context_is_same_speaker_and_same_chapter(self, store, settings):
        result = get_subject_discussion(store, settings, "MB20261028", "Fed")
        live = store.get_live("MB20261028")
        chapters = {s.segment_id: s.chapter_id for s in live.segments}
        for group in result.groups:
            assert {s.speaker_id for s in group.segments} == {group.speaker_id}
            matched_chapters = {
                chapters[s.segment_id] for s in group.segments if s.segment_id in group.matched_segment_ids
            }
            assert {chapters[s.segment_id] for s in group.segments} <= matched_chapters

    def test_matched_segments_are_distinguished_from_context(self, store, settings):
        result = get_subject_discussion(store, settings, "MB20261028", "Fed")
        for group in result.groups:
            assert group.matched_segment_ids
            assert len(group.segments) >= len(group.matched_segment_ids)

    def test_groups_ordered_by_first_mention(self, store, settings):
        result = get_subject_discussion(store, settings, "MB20261028", "Fed")
        firsts = [min(s.start_s for s in g.segments) for g in result.groups]
        assert firsts == sorted(firsts)

    def test_speaker_filter_narrows_to_one_group(self, store, settings):
        result = get_subject_discussion(store, settings, "MB20261028", "Fed", speaker="Braganti")
        assert [g.speaker_id for g in result.groups] == ["peter-braganti"]

    def test_groups_are_capped(self, store, settings):
        """Context expansion must never produce an unbounded list (section 5)."""
        tight = Settings(data_dir=settings.data_dir, max_discussion_segments=2)
        result = get_subject_discussion(store, tight, "MB20261028", "Fed")
        assert all(len(g.segments) <= 2 for g in result.groups)
        assert result.truncated_groups


class TestSpeakerCoverage:
    def test_topics_come_from_the_chapters_headed(self, store):
        result = speaker_coverage(store, "MB20261028", "Braganti")
        assert result.topics_raw == ["Petrolio, oro, dollaro"]

    def test_empty_instruments_are_explained_by_scope(self, store):
        """MB20261028 indexes equities only, so an oil/gold block yields none."""
        result = speaker_coverage(store, "MB20261028", "Braganti")
        assert result.instruments == []
        assert result.mention_index_scope == "equities_only"

    def test_instruments_come_from_the_speakers_own_segments(self, store):
        result = speaker_coverage(store, "MB20261028", "Giancarlo Prisco")
        assert result.instruments
        assert all(i.speaker_ids == ["giancarlo-prisco"] for i in result.instruments)

    def test_unknown_speaker_raises(self, store):
        with pytest.raises(UnknownSpeaker):
            speaker_coverage(store, "MB20261028", "Ada Lovelace")


class TestRankSubjects:
    def test_not_comparable_across_the_two_lives(self, store):
        """Section 9 acceptance."""
        result = rank_subjects(store)
        assert result.comparable is False
        assert any("not comparable" in w.lower() for w in result.warnings)

    def test_single_live_is_comparable(self, store):
        result = rank_subjects(store, date_from="2026-10-29")
        assert result.comparable is True

    def test_measure_is_labelled_as_airtime(self, store):
        result = rank_subjects(store)
        assert result.measure == "mention_count"
        assert any("airtime" in w for w in result.warnings)

    def test_ordering_and_limit(self, store):
        result = rank_subjects(store, limit=3)
        counts = [s.mention_count for s in result.subjects]
        assert len(result.subjects) == 3 and counts == sorted(counts, reverse=True)

    def test_topic_kind_raises_rather_than_inventing_categories(self, store):
        with pytest.raises(TopicRankingUnavailable, match="no topics registry"):
            rank_subjects(store, kind="topic")


class TestRubricOccurrences:
    def test_one_occurrence_per_live(self, store):
        """Section 9 acceptance: Grafico del giorno appears once in each live."""
        result = rubric_occurrences(store, "Grafico del giorno")
        assert len(result.occurrences) == 2
        assert {o.live_id for o in result.occurrences} == {"MB20261028", "MB20261029"}

    def test_speakers_heard_in_the_chapter_are_listed(self, store):
        """A rubric chapter has no heading speaker, so heads alone would be empty."""
        result = rubric_occurrences(store, "Grafico del giorno")
        assert all(o.speaker_names for o in result.occurrences)

    def test_instruments_in_the_chapter_are_listed(self, store):
        result = rubric_occurrences(store, "Grafico del giorno")
        names = {n for o in result.occurrences for n in o.instrument_names}
        assert {"Snam", "F5"} <= names

    def test_accepts_id_name_and_apostrophe_variants(self, store):
        assert resolve_rubric(store, "grafico-del-giorno").rubric_id == "grafico-del-giorno"
        assert resolve_rubric(store, "Chiediamolo all'AI").rubric_id == "chiediamolo-all-ai"
        assert resolve_rubric(store, "Chiediamolo all’AI").rubric_id == "chiediamolo-all-ai"

    def test_unknown_rubric_raises_with_options(self, store):
        with pytest.raises(UnknownRubric):
            rubric_occurrences(store, "Nonexistent Rubric")

    def test_date_range_narrows_occurrences(self, store):
        result = rubric_occurrences(store, "Grafico del giorno", date_from="2026-10-29")
        assert [o.live_id for o in result.occurrences] == ["MB20261029"]


class TestSpeakerResolution:
    def test_ambiguous_surname_is_an_error_not_a_guess(self):
        from easygoing_mcp.models.registries import SpeakerEntry

        class TwoRossi:
            def speakers(self):
                return (
                    SpeakerEntry(speaker_id="mario-rossi", name="Mario Rossi"),
                    SpeakerEntry(speaker_id="lucia-rossi", name="Lucia Rossi"),
                )

        with pytest.raises(AmbiguousSpeaker, match="matches several people"):
            resolve_speaker(TwoRossi(), "Rossi")
