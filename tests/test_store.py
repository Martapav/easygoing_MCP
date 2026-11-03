"""ContentStore behaviour (milestone 1).

Exercised against the real canonical files, because the store's job is to serve
exactly those, plus synthetic directories for the failure paths.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from easygoing_mcp.config import Settings, load_settings
from easygoing_mcp.ingest.slugs import normalize
from easygoing_mcp.store.base import (
    ContentStore,
    SegmentQuery,
    StoreError,
    UnknownInstrument,
    UnknownLive,
    UnknownRubric,
    UnknownSegment,
    UnknownSpeaker,
)
from easygoing_mcp.store.json_store import JsonContentStore

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_DATA = REPO_ROOT / "data"

pytestmark = pytest.mark.skipif(
    not (REAL_DATA / "canonical" / "MB20261028.canonical.json").exists(),
    reason="canonical files not built; run scripts/ingest.py",
)


@pytest.fixture(scope="module")
def store() -> JsonContentStore:
    return JsonContentStore(load_settings({"EASYGOING_DATA_DIR": str(REAL_DATA)}))


@pytest.fixture
def copied_data(tmp_path) -> Path:
    target = tmp_path / "data"
    (target / "canonical").mkdir(parents=True)
    shutil.copytree(REAL_DATA / "registries", target / "registries")
    for path in (REAL_DATA / "canonical").glob("*.canonical.json"):
        shutil.copy(path, target / "canonical" / path.name)
    return target


class TestProtocolConformance:
    def test_json_store_satisfies_the_protocol(self, store):
        assert isinstance(store, ContentStore)

    def test_every_protocol_method_is_implemented(self, store):
        """JsonContentStore deliberately does not inherit the Protocol, so a
        missing method would be a silent AttributeError rather than a stub."""
        expected = [
            name for name in dir(ContentStore)
            if not name.startswith("_") and callable(getattr(ContentStore, name, None))
        ]
        missing = [name for name in expected if not callable(getattr(store, name, None))]
        assert missing == []


class TestLoading:
    def test_missing_canonical_directory_is_a_clear_error(self, tmp_path):
        (tmp_path / "data" / "canonical").mkdir(parents=True)
        shutil.copytree(REAL_DATA / "registries", tmp_path / "data" / "registries")
        with pytest.raises(StoreError, match="no canonical files"):
            JsonContentStore(Settings(data_dir=tmp_path / "data"))

    def test_missing_registry_is_a_clear_error(self, copied_data):
        (copied_data / "registries" / "speakers.json").unlink()
        with pytest.raises(StoreError, match="Run scripts/ingest.py"):
            JsonContentStore(Settings(data_dir=copied_data))

    def test_unregistered_speaker_fails_at_startup(self, copied_data):
        """Better to fail on load than to serve a live whose ids cannot resolve."""
        path = copied_data / "registries" / "speakers.json"
        entries = [e for e in json.loads(path.read_text()) if e["speaker_id"] != "davide-biocchi"]
        path.write_text(json.dumps(entries), encoding="utf-8")
        with pytest.raises(StoreError, match="speakers not in speakers.json"):
            JsonContentStore(Settings(data_dir=copied_data))

    def test_strict_false_skips_the_coverage_check(self, copied_data):
        path = copied_data / "registries" / "speakers.json"
        entries = [e for e in json.loads(path.read_text()) if e["speaker_id"] != "davide-biocchi"]
        path.write_text(json.dumps(entries), encoding="utf-8")
        assert JsonContentStore(Settings(data_dir=copied_data), strict=False).live_ids()


class TestLives:
    def test_lives_are_newest_first(self, store):
        assert list(store.live_ids()) == ["MB20261029", "MB20261028"]

    def test_get_live(self, store):
        assert store.get_live("MB20261028").live.date == "2026-10-28"

    def test_unknown_live_lists_the_alternatives(self, store):
        with pytest.raises(UnknownLive, match="MB20261029"):
            store.get_live("MB20261030")

    def test_date_range_is_inclusive(self, store):
        assert [l.live.live_id for l in store.lives(date_from="2026-10-29")] == ["MB20261029"]
        assert [l.live.live_id for l in store.lives(date_to="2026-10-28")] == ["MB20261028"]
        assert len(store.lives(date_from="2026-10-28", date_to="2026-10-29")) == 2
        assert store.lives(date_from="2026-11-01") == ()

    def test_truncated_live_and_missing_video_url_load_fine(self, store):
        live = store.get_live("MB20261029")
        assert live.coverage.truncated is True
        assert live.live.video_url is None  # no deep links downstream


class TestSegments:
    def test_get_segment(self, store):
        segment = store.get_segment("MB20261028", "MB20261028-001")
        assert segment.start_s == 0

    def test_unknown_segment(self, store):
        with pytest.raises(UnknownSegment):
            store.get_segment("MB20261028", "MB20261028-999")

    def test_zero_duration_segments_are_served(self, store):
        live = store.get_live("MB20261029")
        zero = [s for s in live.segments if s.start_s == s.end_s]
        assert len(zero) == 32  # 31 anomalies plus the truncated final segment

    def test_adjacent_segments_window(self, store):
        window = store.adjacent_segments("MB20261028", "MB20261028-010", before=2, after=2)
        assert [s.segment_id for s in window] == [
            "MB20261028-008", "MB20261028-009", "MB20261028-010",
            "MB20261028-011", "MB20261028-012",
        ]

    def test_adjacent_segments_clamp_at_the_edges(self, store):
        first = store.adjacent_segments("MB20261028", "MB20261028-001", before=5, after=1)
        assert [s.segment_id for s in first] == ["MB20261028-001", "MB20261028-002"]


class TestSearch:
    def test_instrument_filter_uses_the_mention_index(self, store):
        page = store.search_segments(SegmentQuery(instrument_ids=["stellantis"]))
        assert page.total == 17
        assert {h.live_id for h in page.items} == {"MB20261028", "MB20261029"}
        assert all("stellantis" in h.matched_instrument_ids for h in page.items)

    def test_text_match_respects_token_boundaries(self, store):
        """`Oro` must not match `lavoro`; Italian makes substring search wrong."""
        page = store.search_segments(SegmentQuery(text_any=[normalize("oro")]))
        for hit in page.items:
            assert normalize(hit.segment.text).find("oro") >= 0
        naive = sum(
            1
            for live_id in store.live_ids()
            for segment in store.get_live(live_id).segments
            if "oro" in normalize(segment.text)
        )
        assert page.total < naive

    def test_instrument_and_text_combine_as_a_union(self, store):
        """Section 7 resolves a subject with mentions *plus* a text match."""
        by_index = store.search_segments(SegmentQuery(instrument_ids=["stellantis"])).total
        by_text = store.search_segments(SegmentQuery(text_any=["stellantis"])).total
        union = store.search_segments(
            SegmentQuery(instrument_ids=["stellantis"], text_any=["stellantis"])
        ).total
        assert union >= max(by_index, by_text)

    def test_other_filters_combine_as_and(self, store):
        page = store.search_segments(
            SegmentQuery(live_ids=["MB20261028"], instrument_ids=["stellantis"])
        )
        assert page.total == 7
        assert {h.live_id for h in page.items} == {"MB20261028"}

    def test_speaker_filter(self, store):
        page = store.search_segments(
            SegmentQuery(live_ids=["MB20261028"], speaker_ids=["peter-braganti"])
        )
        assert page.total > 0
        assert {h.segment.speaker.speaker_id for h in page.items} == {"peter-braganti"}

    def test_date_filter(self, store):
        page = store.search_segments(SegmentQuery(instrument_ids=["stellantis"], date_to="2026-10-28"))
        assert {h.live_id for h in page.items} == {"MB20261028"}

    def test_ordering_is_date_desc_then_start_s(self, store):
        page = store.search_segments(SegmentQuery(instrument_ids=["stellantis"]))
        keys = [
            (store.get_live(h.live_id).live.date, h.segment.start_s) for h in page.items
        ]
        assert keys == sorted(keys, key=lambda k: (k[0], -k[1]), reverse=True)

    def test_paging(self, store):
        full = store.search_segments(SegmentQuery(instrument_ids=["stellantis"]))
        first = store.search_segments(SegmentQuery(instrument_ids=["stellantis"], limit=5))
        assert len(first.items) == 5
        assert first.total == full.total
        assert first.next_offset == 5

        last = store.search_segments(
            SegmentQuery(instrument_ids=["stellantis"], limit=5, offset=15)
        )
        assert last.next_offset is None  # 17 hits, window 15..17
        assert [h.segment.segment_id for h in last.items] == [
            h.segment.segment_id for h in full.items[15:]
        ]

    def test_without_window_drops_the_window_only(self):
        query = SegmentQuery(live_ids=["A"], text_any=["x"], limit=5, offset=10)
        widened = query.without_window()
        assert widened.limit is None and widened.offset == 0
        assert widened.live_ids == ("A",) or list(widened.live_ids) == ["A"]
        assert list(widened.text_any) == ["x"]

    def test_empty_filters_return_everything(self, store):
        page = store.search_segments(SegmentQuery())
        assert page.total == 137 + 241


class TestAggregates:
    def test_count_mentions(self, store):
        counts = store.count_mentions()
        assert counts["stellantis"] == 17
        assert sum(counts.values()) == 53 + 111

    def test_count_mentions_respects_the_date_range(self, store):
        counts = store.count_mentions(date_from="2026-10-29")
        assert sum(counts.values()) == 111

    def test_live_ids_for_speaker(self, store):
        assert list(store.live_ids_for_speaker("davide-biocchi")) == ["MB20261029", "MB20261028"]

    def test_live_ids_for_a_speaker_with_one_live(self, store):
        assert list(store.live_ids_for_speaker("peter-braganti")) == ["MB20261028"]

    def test_unknown_speaker_raises(self, store):
        with pytest.raises(UnknownSpeaker):
            store.live_ids_for_speaker("ada-lovelace")


class TestRegistries:
    def test_get_speaker_is_uncurated_until_a_human_fills_it(self, store):
        speaker = store.get_speaker("davide-biocchi")
        assert speaker.name == "Davide Biocchi"
        assert speaker.role is None and speaker.affiliation is None
        assert speaker.is_curated is False

    def test_instrument_match_terms(self, store):
        instrument = store.get_instrument("alphabet-google")
        assert instrument.match_terms() == ["Alphabet / Google", "GOOGL", "GOOG"]
        assert instrument.asset_class is None  # never guessed

    def test_unknown_lookups_raise_with_hints(self, store):
        with pytest.raises(UnknownSpeaker):
            store.get_speaker("nobody")
        with pytest.raises(UnknownRubric):
            store.get_rubric("no-such-rubric")
        with pytest.raises(UnknownInstrument):
            store.get_instrument("no-such-instrument")

    def test_listings_are_sorted_and_complete(self, store):
        assert len(store.speakers()) == 13
        assert len(store.rubrics()) == 7
        assert len(store.instruments()) == 50
        assert [p.programme_id for p in store.programmes()] == ["market-briefing"]
