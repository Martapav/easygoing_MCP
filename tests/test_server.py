"""Protocol-level tests for the resources (milestone 2, section 9).

Driven through the SDK's in-memory client, so these exercise real MCP
request/response handling rather than calling the service functions directly.
Both advice-risk configurations are covered, as section 6 requires.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import anyio
import pytest
from mcp import Client

from easygoing_mcp.config import Settings, load_settings
from easygoing_mcp.server import create_server
from easygoing_mcp.store.json_store import JsonContentStore

REAL_DATA = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(
    not (REAL_DATA / "canonical" / "MB20261028.canonical.json").exists(),
    reason="canonical files not built; run scripts/ingest.py",
)


def settings_for(tmp_path: Path | None = None, *, advice_risk: bool = False) -> Settings:
    """Real canonical data, with summaries redirected when a test needs to write one."""
    base = load_settings({"EASYGOING_DATA_DIR": str(REAL_DATA)})
    return Settings(
        data_dir=tmp_path if tmp_path is not None else base.data_dir,
        enable_advice_risk=advice_risk,
    )


def build(*, advice_risk: bool = False, settings: Settings | None = None):
    settings = settings or settings_for(advice_risk=advice_risk)
    store = JsonContentStore(load_settings({"EASYGOING_DATA_DIR": str(REAL_DATA)}))
    return create_server(settings, store)


def read_json(server, uri: str) -> dict:
    async def go():
        async with Client(server) as client:
            result = await client.read_resource(uri)
            return result.contents[0]

    content = anyio.run(go)
    return json.loads(content.text)


def read_text(server, uri: str) -> str:
    async def go():
        async with Client(server) as client:
            result = await client.read_resource(uri)
            return result.contents[0].text

    return anyio.run(go)


def listing(server) -> tuple[list[str], list[str]]:
    async def go():
        async with Client(server) as client:
            resources = await client.list_resources()
            templates = await client.list_resource_templates()
            return (
                [str(r.uri) for r in resources.resources],
                [t.uri_template for t in templates.resource_templates],
            )

    return anyio.run(go)


class TestRegistration:
    def test_static_and_template_resources_are_listed(self):
        resources, templates = listing(build())
        assert "easygoing://lives" in resources
        assert "easygoing://lives/{live_id}/chapters" in templates
        assert "easygoing://lives/{live_id}/instruments" in templates
        assert "easygoing://speakers/{speaker_id}" in templates

    def test_advice_risk_resources_are_absent_when_disabled(self):
        """Section 6: not registered at all, not merely hidden."""
        resources, templates = listing(build(advice_risk=False))
        assert not any("summary" in uri for uri in resources + templates)

    def test_advice_risk_resources_appear_when_enabled(self):
        _, templates = listing(build(advice_risk=True))
        assert "easygoing://lives/{live_id}/summary" in templates

    def test_descriptions_are_written_for_the_model(self):
        async def go():
            async with Client(build()) as client:
                return await client.list_resource_templates()

        for template in anyio.run(go).resource_templates:
            assert template.description and len(template.description) > 80

    def test_resources_declare_json_except_the_summary(self):
        async def go():
            async with Client(build(advice_risk=True)) as client:
                resources = await client.list_resources()
                templates = await client.list_resource_templates()
                return resources.resources, templates.resource_templates

        resources, templates = anyio.run(go)
        for item in [*resources, *templates]:
            uri = str(getattr(item, "uri", "")) or item.uri_template
            expected = "text/markdown" if uri.endswith("/summary") else "application/json"
            assert item.mime_type == expected, uri


class TestLivesResource:
    def test_lives_are_most_recent_first(self):
        payload = read_json(build(), "easygoing://lives")
        assert [l["live_id"] for l in payload["lives"]] == ["MB20261029", "MB20261028"]

    def test_null_title_is_preserved(self):
        payload = read_json(build(), "easygoing://lives")
        by_id = {l["live_id"]: l for l in payload["lives"]}
        assert by_id["MB20261028"]["title"] is None
        assert by_id["MB20261029"]["title"] == "E POI VENNE LA FED..."

    def test_truncation_and_video_availability_are_exposed(self):
        by_id = {l["live_id"]: l for l in read_json(build(), "easygoing://lives")["lives"]}
        assert by_id["MB20261029"]["truncated"] is True
        assert by_id["MB20261029"]["has_video"] is False
        assert by_id["MB20261028"]["has_video"] is True

    def test_guest_speakers_are_listed_without_repeats(self):
        by_id = {l["live_id"]: l for l in read_json(build(), "easygoing://lives")["lives"]}
        guests = by_id["MB20261028"]["guest_speaker_names"]
        assert guests.count("Eugenio Sartorelli") == 1  # heads two blocks


class TestChaptersResource:
    def test_rubrics_announced_around_707_922_s_are_present(self):
        """Section 9 acceptance: MB20261029 chapters include those rubrics."""
        payload = read_json(build(), "easygoing://lives/MB20261029/chapters")
        starts = {c["start_s"]: c for c in payload["chapters"]}
        assert 707 in starts and 922 in starts
        assert starts[707]["rubric_name"] == "Grafico del giorno"

    def test_unverified_chapter_time_is_marked_and_tilde_prefixed(self):
        payload = read_json(build(), "easygoing://lives/MB20261029/chapters")
        unverified = [c for c in payload["chapters"] if c["start_quality"] == "chapter_official_unverified"]
        assert len(unverified) == 1
        assert unverified[0]["timestamp_label"].startswith("~")

    def test_guest_and_rubric_chapters_are_distinguished(self):
        payload = read_json(build(), "easygoing://lives/MB20261028/chapters")
        guests = [c for c in payload["chapters"] if c["speaker_names"]]
        rubrics = [c for c in payload["chapters"] if c["rubric_name"]]
        assert len(guests) == 9 and len(rubrics) == 5
        assert all(not (c["speaker_names"] and c["rubric_name"]) for c in payload["chapters"])

    def test_caveats_are_present_and_data_driven(self):
        payload = read_json(build(), "easygoing://lives/MB20261029/chapters")
        caveats = payload["caveats"]
        assert caveats["truncated"] is True
        assert caveats["timestamps_estimated"] is True
        assert caveats["mention_index_scope"] == "multi_asset"
        assert any("deep links" in note for note in caveats["notes"])

    def test_unknown_live_id_is_an_error_listing_options(self):
        async def go():
            async with Client(build()) as client:
                with pytest.raises(Exception, match="MB20261029"):
                    await client.read_resource("easygoing://lives/MB20261030/chapters")

        anyio.run(go)


class TestInstrumentsResource:
    def test_scope_is_exposed_per_live(self):
        """Counts are not comparable across lives, so scope travels with them."""
        assert read_json(build(), "easygoing://lives/MB20261028/instruments")[
            "mention_index_scope"
        ] == "equities_only"
        assert read_json(build(), "easygoing://lives/MB20261029/instruments")[
            "mention_index_scope"
        ] == "multi_asset"

    def test_first_mention_is_a_segment_ref(self):
        payload = read_json(build(), "easygoing://lives/MB20261028/instruments")
        first = payload["instruments"][0]["first_mention"]
        assert {"segment_id", "timestamp_label", "speaker_name", "text", "deep_link"} <= first.keys()
        assert first["deep_link"].startswith("https://youtube.com/live/")

    def test_no_deep_link_without_a_video_url(self):
        payload = read_json(build(), "easygoing://lives/MB20261029/instruments")
        assert all(i["first_mention"]["deep_link"] is None for i in payload["instruments"])

    def test_asset_class_stays_null_until_curated(self):
        payload = read_json(build(), "easygoing://lives/MB20261028/instruments")
        assert all(i["asset_class"] is None for i in payload["instruments"])


class TestSpeakerResource:
    def test_uncurated_fields_are_explained_not_just_null(self):
        payload = read_json(build(), "easygoing://speakers/peter-braganti")
        assert payload["role"] is None and payload["affiliation"] is None
        assert payload["is_curated"] is False
        assert "not yet curated" in payload["curation_note"]

    def test_appearances_are_listed_newest_first(self):
        payload = read_json(build(), "easygoing://speakers/eugenio-sartorelli")
        assert [a["live_id"] for a in payload["appearances"]] == ["MB20261029", "MB20261028"]

    def test_chapters_headed_are_reported(self):
        payload = read_json(build(), "easygoing://speakers/eugenio-sartorelli")
        by_live = {a["live_id"]: a for a in payload["appearances"]}
        assert len(by_live["MB20261028"]["chapter_ids"]) == 2

    def test_unknown_speaker_is_an_error(self):
        async def go():
            async with Client(build()) as client:
                with pytest.raises(Exception):
                    await client.read_resource("easygoing://speakers/ada-lovelace")

        anyio.run(go)


class TestSummaryResource:
    @pytest.fixture
    def data_with_summaries(self, tmp_path) -> Settings:
        (tmp_path / "derived" / "summaries").mkdir(parents=True)
        return Settings(data_dir=tmp_path, enable_advice_risk=True)

    def test_missing_summary_says_it_was_never_generated(self, data_with_summaries):
        server = build(settings=data_with_summaries)

        async def go():
            async with Client(server) as client:
                with pytest.raises(Exception, match="no summary has been generated"):
                    await client.read_resource("easygoing://lives/MB20261028/summary")

        anyio.run(go)

    def test_existing_summary_is_served_with_the_disclaimer(self, data_with_summaries):
        path = data_with_summaries.summaries_dir / "MB20261028.md"
        path.write_text("# Market Briefing\n\nBiocchi said something.\n", encoding="utf-8")

        body = read_text(build(settings=data_with_summaries), "easygoing://lives/MB20261028/summary")
        assert "not investment advice" in body
        assert "Biocchi said something." in body

    def test_the_server_never_generates_a_summary(self, data_with_summaries):
        """A missing summary must stay missing, not be written on read."""
        server = build(settings=data_with_summaries)

        async def go():
            async with Client(server) as client:
                with contextlib.suppress(Exception):
                    await client.read_resource("easygoing://lives/MB20261029/summary")

        anyio.run(go)
        assert list(data_with_summaries.summaries_dir.iterdir()) == []


class TestStdioHygiene:
    def test_serving_a_request_writes_nothing_to_stdout(self):
        """Section 3: stdout carries the protocol; anything else corrupts it."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            read_json(build(advice_risk=True), "easygoing://lives")
        assert buffer.getvalue() == ""


def call_tool(server, name: str, arguments: dict):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, arguments)

    return anyio.run(go)


def tool_schemas(server) -> dict:
    async def go():
        async with Client(server) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    return anyio.run(go)


class TestToolRegistration:
    def test_non_advice_risk_tools_are_always_present(self):
        names = set(tool_schemas(build()))
        assert {"find_mentions", "get_speaker_coverage", "rank_subjects"} <= names

    def test_advice_risk_tools_are_absent_when_disabled(self):
        """Section 6: not registered at all."""
        names = set(tool_schemas(build(advice_risk=False)))
        assert "get_subject_discussion" not in names
        assert "list_rubric_occurrences" not in names

    def test_advice_risk_tools_appear_when_enabled(self):
        names = set(tool_schemas(build(advice_risk=True)))
        assert {"get_subject_discussion", "list_rubric_occurrences"} <= names

    def test_tools_are_annotated_read_only(self):
        for tool in tool_schemas(build(advice_risk=True)).values():
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True

    def test_parameter_names_come_from_the_catalog(self):
        schemas = tool_schemas(build(advice_risk=True))
        assert set(schemas["find_mentions"].input_schema["properties"]) == {
            "subject", "live_id", "speaker", "date_from", "date_to", "limit", "cursor",
        }
        assert set(schemas["get_subject_discussion"].input_schema["properties"]) == {
            "live_id", "subject", "speaker",
        }
        assert set(schemas["get_speaker_coverage"].input_schema["properties"]) == {
            "live_id", "speaker",
        }
        assert set(schemas["list_rubric_occurrences"].input_schema["properties"]) == {
            "rubric", "date_from", "date_to",
        }

    def test_required_parameters_are_marked_required(self):
        schemas = tool_schemas(build(advice_risk=True))
        assert schemas["find_mentions"].input_schema.get("required") == ["subject"]
        assert set(schemas["get_speaker_coverage"].input_schema.get("required", [])) == {
            "live_id", "speaker",
        }


class TestStructuredOutput:
    """Section 9: structuredContent must validate against outputSchema."""

    @pytest.mark.parametrize(
        "name,arguments",
        [
            ("find_mentions", {"subject": "Stellantis"}),
            ("get_speaker_coverage", {"live_id": "MB20261028", "speaker": "Braganti"}),
            ("rank_subjects", {}),
            ("get_subject_discussion", {"live_id": "MB20261028", "subject": "Fed"}),
            ("list_rubric_occurrences", {"rubric": "Grafico del giorno"}),
        ],
    )
    def test_structured_content_validates_against_output_schema(self, name, arguments):
        import jsonschema

        server = build(advice_risk=True)
        schema = tool_schemas(server)[name].output_schema
        assert schema is not None, f"{name} declares no outputSchema"

        result = call_tool(server, name, arguments)
        assert result.is_error is False
        assert result.structured_content is not None
        jsonschema.validate(result.structured_content, schema)

    def test_text_content_accompanies_the_structured_result(self):
        result = call_tool(build(), "find_mentions", {"subject": "Stellantis"})
        assert result.content and result.content[0].text


class TestToolBehaviourOverTheProtocol:
    def test_find_mentions_spans_both_lives(self):
        result = call_tool(build(), "find_mentions", {"subject": "Stellantis", "limit": 50})
        payload = result.structured_content
        assert {g["live_id"] for g in payload["groups"]} == {"MB20261028", "MB20261029"}

    def test_find_mentions_paginates(self):
        server = build()
        first = call_tool(server, "find_mentions", {"subject": "Stellantis", "limit": 5})
        cursor = first.structured_content["next_cursor"]
        assert cursor
        second = call_tool(
            server, "find_mentions", {"subject": "Stellantis", "limit": 5, "cursor": cursor}
        )
        assert second.structured_content["returned"] == 5

    def test_limit_is_clamped_to_the_configured_maximum(self):
        result = call_tool(build(), "find_mentions", {"subject": "Stellantis", "limit": 9999})
        assert result.structured_content["returned"] <= 50

    def test_rank_subjects_reports_incomparable_scopes(self):
        payload = call_tool(build(), "rank_subjects", {}).structured_content
        assert payload["comparable"] is False
        assert payload["measure"] == "mention_count"

    def test_rank_subjects_rejects_topic_kind(self):
        result = call_tool(build(), "rank_subjects", {"kind": "topic"})
        assert result.is_error is True
        assert "topics registry" in result.content[0].text

    def test_advice_risk_tools_carry_the_disclaimer(self):
        server = build(advice_risk=True)
        for name, arguments in [
            ("get_subject_discussion", {"live_id": "MB20261028", "subject": "Fed"}),
            ("list_rubric_occurrences", {"rubric": "Grafico del giorno"}),
        ]:
            payload = call_tool(server, name, arguments).structured_content
            assert "not investment advice" in payload["disclaimer"]

    def test_non_advice_risk_tools_have_no_disclaimer_field(self):
        payload = call_tool(build(), "find_mentions", {"subject": "Stellantis"}).structured_content
        assert "disclaimer" not in payload

    def test_rubric_occurrences_one_per_live(self):
        payload = call_tool(
            build(advice_risk=True), "list_rubric_occurrences", {"rubric": "Grafico del giorno"}
        ).structured_content
        assert len(payload["occurrences"]) == 2


class TestToolErrors:
    def test_unknown_live_id_message_reaches_the_client(self):
        result = call_tool(build(), "find_mentions", {"subject": "x", "live_id": "MB20261030"})
        assert result.is_error is True
        assert "MB20261029" in result.content[0].text

    def test_unresolvable_subject_says_what_was_searched(self):
        result = call_tool(build(), "find_mentions", {"subject": "zzzzznotathing"})
        assert result.is_error is True
        assert "was not found" in result.content[0].text

    def test_unknown_speaker_lists_valid_options(self):
        result = call_tool(
            build(), "get_speaker_coverage", {"live_id": "MB20261028", "speaker": "Ada Lovelace"}
        )
        assert result.is_error is True
        assert "davide-biocchi" in result.content[0].text

    def test_foreign_cursor_is_rejected(self):
        result = call_tool(
            build(), "find_mentions", {"subject": "Stellantis", "cursor": "not-a-cursor"}
        )
        assert result.is_error is True
