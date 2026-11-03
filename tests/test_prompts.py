"""Prompts (milestone 4): selection services, rendering, and the MCP surface.

Selection is tested on the service functions directly, without MCP (section
2). The protocol tests at the bottom drive the prompts through the SDK's
in-memory client, in both advice-risk configurations (section 6).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import anyio
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from easygoing_mcp.config import Settings, load_settings
from easygoing_mcp.prompts.templates import (
    DISCLAIMER,
    render_compare_prompt,
    render_summarize_prompt,
)
from easygoing_mcp.server import create_server
from easygoing_mcp.services.portions import (
    PortionError,
    cap_segments,
    comparison_material,
    parse_seconds,
    parse_speaker_list,
    resolve_chapter,
    select_portion,
)
from easygoing_mcp.store.base import UnknownLive, UnknownSpeaker
from easygoing_mcp.store.json_store import JsonContentStore

REAL_DATA = Path(__file__).resolve().parents[1] / "data"
HAVE_DATA = (REAL_DATA / "canonical" / "MB20261028.canonical.json").exists()
needs_data = pytest.mark.skipif(not HAVE_DATA, reason="canonical files not built; run ingest")


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings({"EASYGOING_DATA_DIR": str(REAL_DATA)})


@pytest.fixture(scope="module")
def store(settings) -> JsonContentStore:
    return JsonContentStore(settings)


# --- argument parsing (no data needed) --------------------------------------

class TestParseSeconds:
    @pytest.mark.parametrize(
        "raw, expected",
        [("2400", 2400), ("2400.9", 2400), ("40:00", 2400), ("1:05:30", 3930), (" 0 ", 0)],
    )
    def test_accepted_forms(self, raw, expected):
        assert parse_seconds("start_s", raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_absent(self, raw):
        assert parse_seconds("start_s", raw) is None

    @pytest.mark.parametrize("raw", ["abc", "-5", "nan", "1:75", "1:2:3:4"])
    def test_rejected_forms_name_the_argument(self, raw):
        with pytest.raises(PortionError, match="start_s"):
            parse_seconds("start_s", raw)


def test_speaker_list_is_comma_separated():
    assert parse_speaker_list(" Michettoni, Prisco ,,") == ["Michettoni", "Prisco"]
    assert parse_speaker_list(None) == []


# --- capping -----------------------------------------------------------------

@needs_data
class TestCap:
    def test_segment_count_cap(self, store, settings):
        segments = store.get_live("MB20261029").segments
        capped = dataclasses.replace(settings, max_prompt_segments=5)
        kept, was_capped = cap_segments(segments, capped)
        assert len(kept) == 5 and was_capped

    def test_char_cap_keeps_whole_segments(self, store, settings):
        segments = store.get_live("MB20261029").segments
        capped = dataclasses.replace(settings, max_prompt_chars=1000)
        kept, was_capped = cap_segments(segments, capped)
        assert was_capped
        assert sum(len(s.text) for s in kept) <= 1000
        assert all(k.text == s.text for k, s in zip(kept, segments))

    def test_an_oversized_first_segment_is_still_kept(self, store, settings):
        """One MB20261028 segment is ~10,000 chars; an empty prompt is worse than a long one."""
        longest = max(store.get_live("MB20261028").segments, key=lambda s: len(s.text))
        capped = dataclasses.replace(settings, max_prompt_chars=100)
        kept, was_capped = cap_segments([longest, longest], capped)
        assert kept == [longest] and was_capped

    def test_no_cap_when_it_fits(self, store, settings):
        segments = store.get_live("MB20261029").segments[:3]
        assert cap_segments(segments, settings) == (list(segments), False)


# --- REQ-0004 selection ------------------------------------------------------

@needs_data
class TestResolveChapter:
    @pytest.mark.parametrize(
        "value, chapter_id",
        [
            ("MB20261028-C14", "MB20261028-C14"),
            ("mb20261028-c14", "MB20261028-C14"),
            ("14", "MB20261028-C14"),
            ("C3", "MB20261028-C03"),
            ("Grafico del giorno", "MB20261028-C02"),
            ("grafico-del-giorno", "MB20261028-C02"),
            ("Chiediamolo all'AI", "MB20261028-C04"),  # typographic apostrophe in the label
            ("Speciale 0DTE", "MB20261028-C14"),
            ("Michettoni", "MB20261028-C07"),
        ],
    )
    def test_forms(self, store, value, chapter_id):
        live = store.get_live("MB20261028")
        assert resolve_chapter(store, live, value).chapter_id == chapter_id

    def test_two_blocks_for_one_guest_is_an_error_listing_both(self, store):
        live = store.get_live("MB20261028")
        with pytest.raises(PortionError) as exc:
            resolve_chapter(store, live, "Sartorelli")
        assert "MB20261028-C03" in str(exc.value) and "MB20261028-C10" in str(exc.value)

    def test_rubric_absent_from_this_live(self, store):
        live = store.get_live("MB20261028")
        with pytest.raises(PortionError, match="does not occur in MB20261028"):
            resolve_chapter(store, live, "Uno strumento al giorno")

    def test_unknown_chapter_lists_the_options(self, store):
        live = store.get_live("MB20261028")
        with pytest.raises(PortionError, match="MB20261028-C01"):
            resolve_chapter(store, live, "nonexistent block")

    def test_out_of_range_number(self, store):
        with pytest.raises(PortionError, match="no chapter number 99"):
            resolve_chapter(store, store.get_live("MB20261028"), "99")


@needs_data
class TestSelectPortion:
    def test_no_selector_raises(self, store, settings):
        """Section 9 acceptance: summarize_live_portion(live_id="MB20261028") raises."""
        with pytest.raises(PortionError, match="at least one selector"):
            select_portion(store, settings, "MB20261028")

    def test_blank_selectors_count_as_absent(self, store, settings):
        with pytest.raises(PortionError, match="at least one selector"):
            select_portion(store, settings, "MB20261028", chapter=" ", speaker="")

    def test_unknown_live(self, store, settings):
        with pytest.raises(UnknownLive):
            select_portion(store, settings, "MB19990101", chapter="1")

    def test_unknown_speaker(self, store, settings):
        with pytest.raises(UnknownSpeaker):
            select_portion(store, settings, "MB20261028", speaker="Nobody Atall")

    def test_by_chapter(self, store, settings):
        sel = select_portion(store, settings, "MB20261028", chapter="Speciale 0DTE")
        assert {r.segment_id for r in sel.segments} == {
            s.segment_id
            for s in store.get_live("MB20261028").segments
            if s.chapter_id == "MB20261028-C14"
        }
        assert not sel.capped and not sel.reaches_truncated_end

    def test_by_speaker_only_returns_that_speaker(self, store, settings):
        sel = select_portion(store, settings, "MB20261028", speaker="Michettoni")
        assert sel.segments
        assert {r.speaker_id for r in sel.segments} == {"fabio-michettoni"}

    def test_selectors_intersect(self, store, settings):
        chapter_only = select_portion(store, settings, "MB20261028", chapter="Michettoni")
        both = select_portion(
            store, settings, "MB20261028", chapter="Michettoni", speaker="Biocchi"
        )
        assert 0 < len(both.segments) < len(chapter_only.segments)
        assert {r.speaker_id for r in both.segments} == {"davide-biocchi"}

    def test_time_window_overlap(self, store, settings):
        sel = select_portion(store, settings, "MB20261028", start_s="40:00", end_s="55:00")
        assert all(r.start_s <= 3300 for r in sel.segments)
        assert all(r.end_s > 2400 or r.start_s >= 2400 for r in sel.segments)
        # The segment that is already running at 40:00 is included.
        assert sel.segments[0].start_s < 2400

    def test_open_ended_window(self, store, settings):
        sel = select_portion(store, settings, "MB20261028", start_s="5900")
        assert sel.segments[-1].segment_id == store.get_live("MB20261028").segments[-1].segment_id

    def test_inverted_window(self, store, settings):
        with pytest.raises(PortionError, match="after end_s"):
            select_portion(store, settings, "MB20261028", start_s="100", end_s="50")

    def test_window_past_the_truncated_end(self, store, settings):
        with pytest.raises(PortionError, match="truncated"):
            select_portion(store, settings, "MB20261029", start_s="6000")

    def test_empty_intersection_says_when_the_speaker_speaks(self, store, settings):
        with pytest.raises(PortionError, match="speaks in MB20261028 between"):
            select_portion(store, settings, "MB20261028", speaker="Gaudioso", end_s="60")

    def test_truncated_end_is_detected(self, store, settings):
        """MB20261029 stops mid-sentence inside Casarella's block."""
        sel = select_portion(store, settings, "MB20261029", chapter="Casarella")
        assert sel.reaches_truncated_end
        early = select_portion(store, settings, "MB20261029", chapter="Serafini")
        assert not early.reaches_truncated_end

    def test_cap_is_reported(self, store, settings):
        capped = dataclasses.replace(settings, max_prompt_segments=5)
        sel = select_portion(store, capped, "MB20261029", chapter="Casarella")
        assert sel.capped and len(sel.segments) == 5 and sel.total_selected > 5
        # Capped before the end, so the truncated end is not reached by what is shown...
        # ...but the selection does reach it, and that is what the reader must be told.
        assert sel.reaches_truncated_end

    def test_text_is_verbatim(self, store, settings):
        sel = select_portion(store, settings, "MB20261029", chapter="Serafini")
        canonical = {s.segment_id: s.text for s in store.get_live("MB20261029").segments}
        assert all(r.text == canonical[r.segment_id] for r in sel.segments)


# --- REQ-0004 rendering --------------------------------------------------------

@needs_data
class TestSummarizeRendering:
    def test_fixed_instructions_and_disclaimer(self, store, settings):
        text = render_summarize_prompt(
            select_portion(store, settings, "MB20261028", chapter="Speciale 0DTE")
        )
        for rule in ("Attribute every opinion", "NEEDS AUDIO REVIEW", "unverified",
                     "Do not add facts", "truncated end"):
            assert rule in text
        assert text.rstrip().endswith(DISCLAIMER)

    def test_audio_review_flag_is_on_the_line(self, store, settings):
        sel = select_portion(store, settings, "MB20261028", chapter="Sartorelli — Nuova Zelanda")
        flagged = [r for r in sel.segments if r.needs_audio_review]
        assert flagged
        text = render_summarize_prompt(sel)
        transcript = text[text.index("## Transcript"):]
        line = next(l for l in transcript.splitlines() if f"({flagged[0].segment_id};" in l)
        assert "NEEDS AUDIO REVIEW" in line

    def test_truncated_and_estimated_live(self, store, settings):
        text = render_summarize_prompt(
            select_portion(store, settings, "MB20261029", chapter="Casarella")
        )
        assert "TRUNCATED END" in text
        assert "[~" in text  # estimated timestamps are marked
        assert "<http" not in text  # MB20261029 has no video URL, so no deep links

    def test_deep_links_where_a_video_exists(self, store, settings):
        text = render_summarize_prompt(
            select_portion(store, settings, "MB20261028", chapter="Grafico del giorno")
        )
        assert "?t=" in text
        assert "TRUNCATED END" not in text

    def test_capped_selection_says_so(self, store, settings):
        capped = dataclasses.replace(settings, max_prompt_segments=3)
        text = render_summarize_prompt(
            select_portion(store, capped, "MB20261028", chapter="Speciale 0DTE")
        )
        assert "CAPPED" in text and "3 of 37" in text


# --- REQ-0013 ------------------------------------------------------------------

@needs_data
class TestComparisonMaterial:
    def test_all_speakers_when_none_given(self, store, settings):
        material = comparison_material(
            store, settings, "MB20261028", "Fed", None, disclaimer=DISCLAIMER
        )
        names = {g.speaker_id for g in material.discussion.groups}
        assert {"fabio-michettoni", "peter-braganti"} <= names
        assert material.discussion.disclaimer == DISCLAIMER

    def test_requested_speakers_filter(self, store, settings):
        material = comparison_material(
            store, settings, "MB20261028", "Fed", "Michettoni, Braganti", disclaimer=DISCLAIMER
        )
        assert [g.speaker_id for g in material.discussion.groups] == [
            "fabio-michettoni", "peter-braganti"
        ]
        assert material.silent == [] and material.absent == []

    def test_duplicate_names_resolve_once(self, store, settings):
        material = comparison_material(
            store, settings, "MB20261028", "Fed", "Michettoni, fabio-michettoni",
            disclaimer=DISCLAIMER,
        )
        assert len(material.requested) == 1

    def test_silent_and_absent_are_distinguished(self, store, settings):
        material = comparison_material(
            store, settings, "MB20261029", "Tobin Tax", "Cartisano, Rea, Gaudioso",
            disclaimer=DISCLAIMER,
        )
        assert [e.speaker_id for e in material.silent] == ["massimo-rea"]
        assert [e.speaker_id for e in material.absent] == ["fabio-gaudioso"]

    def test_tobin_tax_finds_every_speaker_who_said_it(self, store, settings):
        """Catalog REQ-0013 evidence: Tobin Tax across 1497-1726, 2828-2891, 3580-3696 s."""
        material = comparison_material(
            store, settings, "MB20261029", "Tobin Tax", None, disclaimer=DISCLAIMER
        )
        assert {"davide-biocchi", "paolo-serafini", "andrea-cartisano"} <= {
            g.speaker_id for g in material.discussion.groups
        }

    def test_nobody_requested_discussed_it(self, store, settings):
        with pytest.raises(PortionError, match="Speakers who did"):
            comparison_material(
                store, settings, "MB20261029", "Tobin Tax", "Rea", disclaimer=DISCLAIMER
            )

    def test_cap_drops_context_first_and_keeps_every_speaker(self, store, settings):
        full = comparison_material(store, settings, "MB20261028", "Fed", None, disclaimer="")
        capped_settings = dataclasses.replace(settings, max_prompt_segments=6)
        capped = comparison_material(
            store, capped_settings, "MB20261028", "Fed", None, disclaimer=""
        )
        assert capped.dropped_context
        assert [g.speaker_id for g in capped.discussion.groups] == [
            g.speaker_id for g in full.discussion.groups
        ]
        for g in capped.discussion.groups:
            assert g.segments
            assert {s.segment_id for s in g.segments} <= set(g.matched_segment_ids)
        assert sum(len(g.segments) for g in capped.discussion.groups) <= 6 + len(
            capped.discussion.groups
        )

    def test_caveats_cover_only_embedded_segments(self, store, settings):
        material = comparison_material(
            store, settings, "MB20261028", "Fed", "Michettoni", disclaimer=""
        )
        embedded = {s.segment_id for g in material.discussion.groups for s in g.segments}
        for anomaly in material.discussion.caveats.anomalies:
            assert not anomaly.segment_ids or set(anomaly.segment_ids) & embedded


@needs_data
class TestCompareRendering:
    def test_match_and_context_tags(self, store, settings):
        text = render_compare_prompt(
            comparison_material(store, settings, "MB20261028", "Fed", None, disclaimer=DISCLAIMER)
        )
        assert "[match]" in text and "[context]" in text
        assert "Also searched for: 'federal reserve'" in text
        assert "no clear position" in text
        assert text.rstrip().endswith(DISCLAIMER)

    def test_single_speaker_says_no_comparison(self, store, settings):
        text = render_compare_prompt(
            comparison_material(
                store, settings, "MB20261029", "Tobin Tax", "Cartisano, Gaudioso",
                disclaimer=DISCLAIMER,
            )
        )
        assert "no comparison is possible" in text
        assert "not speaking in this live at all: Fabio Gaudioso" in text


# --- MCP surface ---------------------------------------------------------------

def build(*, advice_risk: bool):
    base = load_settings({"EASYGOING_DATA_DIR": str(REAL_DATA)})
    return create_server(dataclasses.replace(base, enable_advice_risk=advice_risk))


def list_prompts(server):
    async def go():
        async with Client(server) as client:
            return {p.name: p for p in (await client.list_prompts()).prompts}

    return anyio.run(go)


def get_prompt(server, name: str, arguments: dict):
    """Render a prompt; a protocol error is re-raised outside the client's task group,
    which would otherwise wrap it in an ExceptionGroup on exit."""

    async def go():
        async with Client(server) as client:
            try:
                return await client.get_prompt(name, arguments)
            except MCPError as exc:
                return exc

    result = anyio.run(go)
    if isinstance(result, MCPError):
        raise result
    return result


@needs_data
class TestPromptProtocol:
    def test_absent_when_advice_risk_disabled(self):
        """Section 6: not registered at all."""
        assert list_prompts(build(advice_risk=False)) == {}

    def test_present_with_catalog_arguments_when_enabled(self):
        prompts = list_prompts(build(advice_risk=True))
        assert set(prompts) == {"summarize_live_portion", "compare_views"}
        args = {a.name: a.required for a in prompts["summarize_live_portion"].arguments}
        assert args == {
            "live_id": True, "chapter": False, "speaker": False, "start_s": False, "end_s": False
        }
        args = {a.name: a.required for a in prompts["compare_views"].arguments}
        assert args == {"live_id": True, "subject": True, "speakers": False}
        for prompt in prompts.values():
            assert prompt.description and len(prompt.description) > 80
            assert all(a.description for a in prompt.arguments)

    def test_summarize_renders_one_user_message(self):
        result = get_prompt(
            build(advice_risk=True), "summarize_live_portion",
            {"live_id": "MB20261028", "chapter": "Speciale 0DTE"},
        )
        assert len(result.messages) == 1
        message = result.messages[0]
        assert message.role == "user"
        assert "Fabio Gaudioso" in message.content.text
        assert DISCLAIMER in message.content.text

    def test_numeric_arguments_arrive_as_strings(self):
        result = get_prompt(
            build(advice_risk=True), "summarize_live_portion",
            {"live_id": "MB20261028", "start_s": "2400", "end_s": "3300"},
        )
        assert "time window 00:40:00–00:55:00" in result.messages[0].content.text

    def test_no_selector_error_reaches_the_client_with_its_message(self):
        """Section 9 acceptance, at the protocol level; the message lists the chapters."""
        with pytest.raises(MCPError) as exc:
            get_prompt(build(advice_risk=True), "summarize_live_portion", {"live_id": "MB20261028"})
        assert exc.value.code == -32602  # invalid params
        assert "at least one selector" in str(exc.value)
        assert "MB20261028-C14" in str(exc.value)

    def test_unknown_live_error_lists_valid_ids(self):
        with pytest.raises(MCPError) as exc:
            get_prompt(
                build(advice_risk=True), "compare_views", {"live_id": "MB1", "subject": "Fed"}
            )
        assert "MB20261028" in str(exc.value)

    def test_compare_views_renders(self):
        result = get_prompt(
            build(advice_risk=True), "compare_views",
            {"live_id": "MB20261028", "subject": "Fed", "speakers": "Michettoni, Braganti"},
        )
        text = result.messages[0].content.text
        assert "### Fabio Michettoni" in text and "### Peter Braganti" in text
        assert "### Giancarlo Prisco" not in text
