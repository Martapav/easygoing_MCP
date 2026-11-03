"""Subject resolution and variant expansion (section 7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from easygoing_mcp.config import load_settings
from easygoing_mcp.models.registries import InstrumentEntry
from easygoing_mcp.services.subjects import (
    AmbiguousSubject,
    SubjectError,
    UnresolvableSubject,
    expand_variants,
    instrument_index,
    resolve_subject,
    subject_query,
)
from easygoing_mcp.store.base import SegmentPage, SegmentQuery
from easygoing_mcp.store.json_store import JsonContentStore

REAL_DATA = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(
    not (REAL_DATA / "canonical" / "MB20261028.canonical.json").exists(),
    reason="canonical files not built; run scripts/ingest.py",
)


@pytest.fixture(scope="module")
def store() -> JsonContentStore:
    return JsonContentStore(load_settings({"EASYGOING_DATA_DIR": str(REAL_DATA)}))


class FakeStore:
    """Minimal store for the paths that only need the instrument registry."""

    def __init__(self, instruments: list[InstrumentEntry]) -> None:
        self._instruments = {i.instrument_id: i for i in instruments}

    def instruments(self):
        return tuple(self._instruments.values())

    def get_instrument(self, instrument_id: str) -> InstrumentEntry:
        return self._instruments[instrument_id]

    def search_segments(self, query: SegmentQuery) -> SegmentPage:
        return SegmentPage(items=(), total=0, offset=0)


class TestVariantExpansion:
    def test_abbreviation_expands_to_the_full_name(self):
        assert expand_variants("Fed") == ("fed", "federal reserve")

    def test_expansion_is_bidirectional(self):
        assert expand_variants("Federal Reserve") == ("federal reserve", "fed")

    def test_the_literal_form_comes_first(self):
        assert expand_variants("BCE")[0] == "bce"

    def test_an_unknown_subject_expands_to_itself(self):
        assert expand_variants("Stellantis") == ("stellantis",)

    def test_fomc_is_deliberately_not_folded_into_fed(self):
        """The committee is not the bank; conflating them would move a view
        about one onto the other."""
        assert "fomc" not in expand_variants("Fed")
        assert expand_variants("FOMC") == ("fomc",)


class TestRegistryResolution:
    def test_exact_name(self, store):
        resolved = resolve_subject(store, "Stellantis")
        assert resolved.match_kind == "registry"
        assert resolved.instrument_ids == ("stellantis",)

    def test_ticker(self, store):
        resolved = resolve_subject(store, "STLAM")
        assert resolved.instrument_ids == ("stellantis",)

    def test_one_side_of_a_slash_name(self, store):
        """`Alphabet / Google` names one instrument two ways."""
        resolved = resolve_subject(store, "Google")
        assert resolved.instrument_ids == ("alphabet-google",)
        assert resolved.label == "Alphabet / Google"

    def test_the_other_side_too(self, store):
        assert resolve_subject(store, "Telecom Italia").instrument_ids == ("telecom-italia-tim",)
        assert resolve_subject(store, "TIM").instrument_ids == ("telecom-italia-tim",)

    def test_case_and_accents_are_ignored(self, store):
        assert resolve_subject(store, "stellantis").instrument_ids == ("stellantis",)
        assert resolve_subject(store, "  STELLANTIS ").instrument_ids == ("stellantis",)

    def test_the_asked_form_leads_the_terms(self, store):
        assert resolve_subject(store, "Google").terms[0] == "google"

    def test_real_registry_has_no_ambiguous_terms(self, store):
        assert {t: i for t, i in instrument_index(store).items() if len(i) > 1} == {}


class TestTextResolution:
    def test_non_instrument_subject_falls_back_to_text(self, store):
        resolved = resolve_subject(store, "Fed")
        assert resolved.match_kind == "text"
        assert resolved.instrument_ids == ()
        assert resolved.variants_used == ("federal reserve",)

    def test_variants_are_reported_not_silent(self, store):
        """A response has to be able to say it searched for something the user
        did not type."""
        assert resolve_subject(store, "Fed").variants_used == ("federal reserve",)
        assert resolve_subject(store, "Tobin Tax").variants_used == ()


class TestFailures:
    def test_empty_subject(self, store):
        with pytest.raises(SubjectError, match="empty"):
            resolve_subject(store, "   ")

    def test_unresolvable_subject_lists_what_was_searched(self, store):
        with pytest.raises(UnresolvableSubject, match="was not found"):
            resolve_subject(store, "zzzzznotathing")

    def test_unresolvable_subject_suggests_close_instruments(self, store):
        with pytest.raises(UnresolvableSubject, match="Stellantis"):
            resolve_subject(store, "Stellantys")

    def test_ambiguous_registry_match_lists_candidates(self):
        fake = FakeStore([
            InstrumentEntry(instrument_id="acme-gold", name="Acme / Gold"),
            InstrumentEntry(instrument_id="gold-corp", name="Gold"),
        ])
        with pytest.raises(AmbiguousSubject, match="matches several instruments"):
            resolve_subject(fake, "Gold")

    def test_verify_false_skips_the_existence_check(self, store):
        resolved = resolve_subject(store, "zzzzznotathing", verify=False)
        assert resolved.match_kind == "text"


class TestSubjectQuery:
    def test_registry_subject_queries_both_routes(self, store):
        query = subject_query(resolve_subject(store, "Stellantis"))
        assert query.instrument_ids == ("stellantis",)
        assert "stellantis" in query.text_any

    def test_text_subject_queries_text_only(self, store):
        query = subject_query(resolve_subject(store, "Fed"))
        assert query.instrument_ids is None
        assert list(query.text_any) == ["fed", "federal reserve"]

    def test_filters_pass_through(self, store):
        query = subject_query(resolve_subject(store, "Fed"), live_ids=["MB20261028"], limit=5)
        assert query.live_ids == ["MB20261028"] and query.limit == 5


class TestFedRegression:
    """The case variant expansion exists for (section 9, REQ-0007 evidence)."""

    WINDOWS = ((2615, 2990), (3476, 3581))
    CATALOG_WINDOWS = ((2615, 2990), (3476, 3581), (4167, 4408), (4882, 5110))

    def _speakers_in(self, store, windows):
        page = store.search_segments(
            subject_query(resolve_subject(store, "Fed"), live_ids=["MB20261028"])
        )
        return {
            hit.segment.speaker.speaker_id
            for hit in page.items
            for a, b in windows
            if hit.segment.start_s < b and hit.segment.end_s > a
        }

    def test_two_distinct_speakers_in_the_section_9_windows(self, store):
        assert len(self._speakers_in(store, self.WINDOWS)) >= 2

    def test_braganti_is_reached_only_via_the_variant(self, store):
        """He says "Federal Reserve", never "Fed"."""
        assert "peter-braganti" in self._speakers_in(store, self.WINDOWS)
        literal_only = store.search_segments(
            SegmentQuery(live_ids=["MB20261028"], text_any=["fed"])
        )
        assert "peter-braganti" not in {h.segment.speaker.speaker_id for h in literal_only.items}

    def test_all_four_catalog_speakers_are_found(self, store):
        """The catalog notes "Fed decision discussed by Michettoni, Braganti,
        Prisco and Gaudioso"."""
        assert self._speakers_in(store, self.CATALOG_WINDOWS) == {
            "fabio-michettoni", "peter-braganti", "giancarlo-prisco", "fabio-gaudioso",
        }
