"""Acceptance tests against the real canonical files (section 9, milestone 5).

Each test is derived from the catalog's `evidence` and runs through the MCP
surface (the SDK's in-memory client), so it checks what a host actually sees.

`TestSection9` holds the seven checks section 9 lists, written as stated.
`TestCatalogEvidence` adds stricter checks taken from the same evidence. They
are kept apart on purpose: if the data ever disagrees with a catalog note, the
failure names the discrepancy instead of the section 9 assertion being
weakened to fit (section 9: "Do not weaken the assertion silently").

Every test runs twice, once per canonical source: `json` (data/canonical, the
JSON adapters) and `txt` (data/canonical_txt, the standard .txt route). The
txt run is skipped when data/canonical_txt has not been built.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import anyio
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from easygoing_mcp.config import load_settings
from easygoing_mcp.server import create_server

REAL_DATA = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(
    not (REAL_DATA / "canonical" / "MB20261028.canonical.json").exists(),
    reason="canonical files not built; run scripts/ingest.py",
)

# Which canonical source the module-level helpers serve; set per test below.
_SOURCE = {"value": "json"}


@pytest.fixture(autouse=True, params=["json", "txt"])
def canonical_source(request):
    if request.param == "txt" and not (REAL_DATA / "canonical_txt" / "MB20261028.canonical.json").exists():
        pytest.skip("txt-route canonical files not built; run scripts/ingest.py --route txt")
    _SOURCE["value"] = request.param
    yield request.param
    _SOURCE["value"] = "json"

ADVICE_RISK_TOOLS = {"get_subject_discussion", "list_rubric_occurrences"}
ADVICE_RISK_PROMPTS = {"summarize_live_portion", "compare_views"}
ADVICE_RISK_TEMPLATES = {"easygoing://lives/{live_id}/summary"}


# --- helpers ------------------------------------------------------------------

def server(*, advice_risk: bool = True):
    base = load_settings(
        {"EASYGOING_DATA_DIR": str(REAL_DATA), "EASYGOING_CANONICAL_SOURCE": _SOURCE["value"]}
    )
    return create_server(dataclasses.replace(base, enable_advice_risk=advice_risk))


def _run(coro_fn):
    """Run one client session; re-raise protocol errors outside the task group."""

    async def go():
        async with Client(server()) as client:
            try:
                return await coro_fn(client)
            except MCPError as exc:
                return exc

    result = anyio.run(go)
    if isinstance(result, MCPError):
        raise result
    return result


def call(name: str, arguments: dict) -> dict:
    async def go(client):
        return await client.call_tool(name, arguments)

    result = _run(go)
    assert result.is_error is False, result.content[0].text
    return result.structured_content


def read(uri: str) -> dict:
    async def go(client):
        return await client.read_resource(uri)

    return json.loads(_run(go).contents[0].text)


def all_mentions(subject: str, **arguments) -> list[dict]:
    """Every SegmentRef find_mentions returns, following next_cursor to the end."""
    refs: list[dict] = []
    cursor = None
    for _ in range(100):  # a bound, so a cursor bug cannot loop forever
        args = {"subject": subject, "limit": 50, **arguments}
        if cursor:
            args["cursor"] = cursor
        payload = call("find_mentions", args)
        refs.extend(ref for group in payload["groups"] for ref in group["segments"])
        cursor = payload["next_cursor"]
        if not cursor:
            return refs
    raise AssertionError("find_mentions did not finish paging")


def overlaps(ref: dict, start: int, end: int) -> bool:
    """A segment overlaps a catalog window [start, end], inclusive.

    Inclusive because catalog windows are written as chunk boundaries and
    several segments are zero-duration.
    """
    return ref["start_s"] <= end and ref["end_s"] >= start


# --- the seven section 9 checks ------------------------------------------------

class TestSection9:
    def test_find_mentions_stellantis_spans_both_lives_with_evidence_windows(self):
        refs = all_mentions("Stellantis")
        by_live = {live: [r for r in refs if r["live_id"] == live] for live in
                   ("MB20261028", "MB20261029")}
        assert by_live["MB20261028"] and by_live["MB20261029"]
        assert any(overlaps(r, 1496, 1651) for r in by_live["MB20261028"])
        assert any(overlaps(r, 2605, 2722) for r in by_live["MB20261029"])

    def test_get_subject_discussion_fed_has_two_speakers_in_evidence_windows(self):
        payload = call("get_subject_discussion", {"live_id": "MB20261028", "subject": "Fed"})
        speakers_in_windows = {
            group["speaker_id"]
            for group in payload["groups"]
            for ref in group["segments"]
            if overlaps(ref, 2615, 2990) or overlaps(ref, 3476, 3581)
        }
        assert len(speakers_in_windows) >= 2, speakers_in_windows

    def test_chapters_of_mb20261029_include_rubrics_announced_707_to_922(self):
        chapters = read("easygoing://lives/MB20261029/chapters")["chapters"]
        in_window = [c for c in chapters if 707 <= c["start_s"] <= 922]
        assert any(c["rubric_id"] == "grafico-del-giorno" for c in in_window)
        # 910-922 s is the hand-off to the guest block that follows the rubric.
        assert any(c["start_s"] == 922 and c["speaker_ids"] for c in in_window)

    def test_rank_subjects_over_both_lives_is_not_comparable(self):
        payload = call("rank_subjects", {})
        assert set(payload["live_ids_covered"]) == {"MB20261028", "MB20261029"}
        assert payload["comparable"] is False
        assert payload["measure"] == "mention_count"

    def test_grafico_del_giorno_occurs_once_per_live(self):
        payload = call("list_rubric_occurrences", {"rubric": "Grafico del giorno"})
        lives = [o["live_id"] for o in payload["occurrences"]]
        assert sorted(lives) == ["MB20261028", "MB20261029"]

    def test_summarize_live_portion_without_selector_raises(self):
        async def go(client):
            return await client.get_prompt("summarize_live_portion", {"live_id": "MB20261028"})

        with pytest.raises(MCPError, match="at least one selector"):
            _run(go)

    def test_advice_risk_primitives_absent_when_disabled(self):
        async def listing(client):
            return (
                {t.name for t in (await client.list_tools()).tools},
                {p.name for p in (await client.list_prompts()).prompts},
                {t.uri_template for t in (await client.list_resource_templates()).resource_templates},
            )

        async def go():
            async with Client(server(advice_risk=False)) as off, Client(server()) as on:
                return await listing(off), await listing(on)

        (tools, prompts, templates), (tools_on, prompts_on, templates_on) = anyio.run(go)
        assert not tools & ADVICE_RISK_TOOLS
        assert not prompts & ADVICE_RISK_PROMPTS
        assert not templates & ADVICE_RISK_TEMPLATES
        # ...and present when enabled, so the absence above is the flag's doing.
        assert ADVICE_RISK_TOOLS <= tools_on
        assert ADVICE_RISK_PROMPTS <= prompts_on
        assert ADVICE_RISK_TEMPLATES <= templates_on


# --- stricter checks from the same catalog evidence -----------------------------

class TestCatalogEvidence:
    @pytest.mark.parametrize(
        "live_id, windows",
        [
            # The catalog's fourth MB20261028 window, 2530-2537 s, is an accepted limitation:
            # see test_word_split_across_segments_is_not_matched below.
            ("MB20261028", [(0, 640), (1496, 1651), (4744, 4880)]),
            ("MB20261029", [(68, 144), (2605, 2722), (3502, 3547), (4783, 4815)]),
        ],
    )
    def test_stellantis_recurs_in_all_four_places(self, live_id, windows):
        """REQ-0005: "Stellantis recurs in four separate places" in each live."""
        refs = all_mentions("Stellantis", live_id=live_id)
        missing = [w for w in windows if not any(overlaps(r, *w) for r in refs)]
        assert not missing, f"no Stellantis hit in {live_id} windows {missing}"

    def test_word_split_across_segments_is_not_matched(self):
        """Accepted limitation (decided 2026-11-03), pinned so it cannot change unnoticed.

        REQ-0005 evidence lists Stellantis at 2530-2537 s in MB20261028, but ASR
        chunking split the word: "Stellant" ends MB20261028-027 and "is" starts -028.
        The source securities_index lists neither. Matching across segment
        boundaries was considered and rejected: joining adjacent segments would
        create false hits (a segment ending "F" followed by one starting "ed").
        """
        hits = {r["segment_id"] for r in all_mentions("Stellantis", live_id="MB20261028")}
        assert not {"MB20261028-027", "MB20261028-028"} & hits

    def test_fed_discussion_names_all_four_catalog_speakers(self):
        """REQ-0007: Fed discussed by Michettoni, Braganti, Prisco and Gaudioso."""
        payload = call("get_subject_discussion", {"live_id": "MB20261028", "subject": "Fed"})
        speakers = {g["speaker_id"] for g in payload["groups"]}
        assert {"fabio-michettoni", "peter-braganti", "giancarlo-prisco", "fabio-gaudioso"} <= speakers

    def test_fed_discussion_in_mb20261029_names_the_catalog_speakers(self):
        """REQ-0007: Fed discussed by Biocchi, Serafini, Sartorelli and Cioli Puviani."""
        payload = call("get_subject_discussion", {"live_id": "MB20261029", "subject": "Fed"})
        speakers = {g["speaker_id"] for g in payload["groups"]}
        assert {
            "davide-biocchi", "paolo-serafini", "eugenio-sartorelli", "antonio-cioli-puviani"
        } <= speakers

    def test_grafico_del_giorno_features_the_catalog_instruments(self):
        """REQ-0017: Grafico del giorno features Snam (MB20261028) and F5 (MB20261029)."""
        payload = call("list_rubric_occurrences", {"rubric": "Grafico del giorno"})
        featured = {o["live_id"]: " ".join(o["instrument_names"]).casefold()
                    for o in payload["occurrences"]}
        assert "snam" in featured["MB20261028"]
        assert "f5" in featured["MB20261029"]

    def test_mb20261029_announced_rubrics_are_all_chapters(self):
        """REQ-0002: the host announces each rubric; every one is a chapter."""
        chapters = read("easygoing://lives/MB20261029/chapters")["chapters"]
        rubrics = {c["rubric_id"] for c in chapters if c["rubric_id"]}
        assert {
            "grafico-del-giorno", "chiediamolo-all-ai", "la-voce-dell-agora",
            "spazio-alle-grida", "uno-strumento-al-giorno", "twbook-time",
        } <= rubrics

    def test_mb20261029_has_no_deep_links_and_says_so(self):
        """Section 4.6: no video URL, so no deep links, and the caveats say why."""
        payload = call("find_mentions", {"subject": "Stellantis", "live_id": "MB20261029"})
        group = payload["groups"][0]
        assert all(r["deep_link"] is None for r in group["segments"])
        assert all(r["timestamp_label"].startswith("~") for r in group["segments"])
        assert group["caveats"]["truncated"] is True
        assert group["caveats"]["timestamps_estimated"] is True
