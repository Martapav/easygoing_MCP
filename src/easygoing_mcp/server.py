"""MCP wiring: decorators only, no business logic (section 2).

Everything here translates between the MCP surface and `services/`. Resource
URI templates and names come from `data/catalog.json` verbatim (section 1).

Descriptions are written for the model: what the primitive returns, when to
reach for it instead of a sibling, and the caveats that make its output
unusual. They are based on the catalog's `canonical_formulation` (section 11).

stdio transport: stdout belongs to the protocol, so logging goes to stderr and
nothing calls `print()` (section 3).
"""

from __future__ import annotations

import functools
import logging
import sys
from collections.abc import Callable
from typing import Annotated, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, ToolAnnotations
from pydantic import Field

from .config import Settings, load_settings
from .models.outputs import (
    FindMentionsResult,
    RankSubjectsResult,
    RubricOccurrencesResult,
    SpeakerCoverageResult,
    SubjectDiscussionResult,
)
from .prompts.templates import (
    DISCLAIMER,
    SUMMARY_HEADER,
    render_compare_prompt,
    render_summarize_prompt,
)
from .services.chapters import chapters_view
from .services.lives import SummaryNotGenerated, lives_index, read_summary
from .services.mentions import TopicRankingUnavailable, instruments_view, rank_subjects
from .services.portions import PortionError, comparison_material, select_portion
from .services.rubrics import rubric_occurrences
from .services.search import InvalidCursor, find_mentions, get_subject_discussion
from .services.speakers import AmbiguousSpeaker, speaker_coverage, speaker_view
from .services.subjects import SubjectError
from .store.base import ContentStore, StoreError, UnknownId
from .store.json_store import JsonContentStore

logger = logging.getLogger("easygoing_mcp")

_F = TypeVar("_F", bound=Callable[..., object])


def _clear_errors(fn: _F) -> _F:
    """Surface our error messages instead of letting the SDK replace them.

    A resource template handler that raises anything other than `ResourceError`
    or `MCPError` gets wrapped by the SDK in `UnexpectedResourceError("Error
    creating resource from template ...")`, which discards the message. Section
    5 requires the message to list the valid options or closest matches so a
    model that guessed an id can retry, so the store lookup errors are translated
    here into the exception type the SDK passes through untouched.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (UnknownId, SummaryNotGenerated) as exc:
            # The requested instance does not exist -> -32602 invalid params.
            raise ResourceNotFoundError(str(exc)) from exc
        except StoreError as exc:
            raise ResourceError(str(exc)) from exc

    return wrapper  # type: ignore[return-value]


# Failures we anticipate and whose message the caller needs in order to retry:
# a bad id, an ambiguous or unresolvable subject, a foreign cursor, a ranking
# kind with no data behind it.
_ANTICIPATED: tuple[type[Exception], ...] = (
    StoreError,
    SubjectError,
    AmbiguousSpeaker,
    TopicRankingUnavailable,
    InvalidCursor,
    SummaryNotGenerated,
    PortionError,
)


def _clear_tool_errors(fn: _F) -> _F:
    """Keep our error text on the wire for tools.

    `Tool.run` only preserves the message of a `ToolError` or `ResourceError`;
    anything else becomes `UnexpectedToolError("Error executing tool <name>")`
    and, in the SDK's own words, "nothing from an unexpected exception reaches
    the client". Section 5 requires the message to list the valid options, so
    anticipated failures are re-raised as `ToolError`. Genuine crashes are left
    alone, so a bug still looks like a bug rather than a usage error.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _ANTICIPATED as exc:
            raise ToolError(str(exc)) from exc

    return wrapper  # type: ignore[return-value]


def _clear_prompt_errors(fn: _F) -> _F:
    """Keep our error text on the wire for prompts.

    `Prompt.render` re-raises anything but `MCPError` as `ValueError("Error
    rendering prompt <name>")`, dropping the message, so a missing selector or a
    bad live_id would reach the client with no hint of what to fix. Anticipated
    failures are raised as an `MCPError` with code `-32602` (invalid params),
    which the SDK passes through with its message intact (section 5).
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _ANTICIPATED as exc:
            raise MCPError(INVALID_PARAMS, str(exc)) from exc

    return wrapper  # type: ignore[return-value]


INSTRUCTIONS = """\
Transcripts of EasyGoing live market broadcasts (Market Briefing).

What this server can answer: who said what, when, and about which instrument,
always as attributed quotes with timestamps. Every response about a live carries
caveats describing that live's limitations.

What it cannot answer: it holds no market prices, fundamentals, calendars or
glossary, and no extracted price levels, figures, events or disclosed positions.
Numbers inside quoted text come from automatic transcription and are unverified;
do not treat them as data, and do not restate a speaker's opinion as fact.
"""


def create_server(settings: Settings | None = None, store: ContentStore | None = None) -> MCPServer:
    """Build the server.

    A factory rather than a module-level singleton: section 6 requires the test
    suite to run both advice-risk configurations, and registration differs
    between them — when the flag is false those primitives are not registered at
    all, since hiding them in a description is not enough.
    """
    settings = settings or load_settings()
    store = store if store is not None else JsonContentStore(settings)

    server = MCPServer(name="easygoing", instructions=INSTRUCTIONS)

    @server.resource(
        "easygoing://lives",
        name="lives",
        title="Available lives",
        mime_type="application/json",
        description=(
            "Index of available live broadcasts, most recent first: live_id, date, title, "
            "programme, the guest speakers who headed a block, chapter and segment counts, "
            "whether the source is truncated, and whether the live has a video URL. "
            "Start here to find a live_id for the other resources and tools. Note that title "
            "is null for lives whose source carried none, and that a live without a video URL "
            "can never produce deep links."
        ),
    )
    @_clear_errors
    def lives_resource():
        return lives_index(store)

    @server.resource(
        "easygoing://lives/{live_id}/chapters",
        name="live_chapters",
        title="Chapters of a live",
        mime_type="application/json",
        description=(
            "The running order of one live: each chapter's start time, the provenance of that "
            "start time, whether it is a rubric or a guest block, the speakers, the raw topic "
            "text as broadcast, and how many segments fall in it. Use this to see the shape of "
            "a live or to find when a rubric or guest started. Read start_quality before "
            "quoting a time: a chapter marked chapter_official_unverified carries a published "
            "time the source itself flagged, and in MB20261029 one such time disagrees with the "
            "transcript by about five minutes. topics_raw is free text, not a topic taxonomy."
        ),
    )
    @_clear_errors
    def chapters_resource(live_id: str):
        return chapters_view(store, live_id)

    @server.resource(
        "easygoing://lives/{live_id}/instruments",
        name="live_instruments",
        title="Instruments discussed in a live",
        mime_type="application/json",
        description=(
            "Instruments the source mention index links to one live, with the speakers who "
            "discussed each, the mention count, and the first mention as a segment reference. "
            "Use this to see what a live covered; use find_mentions to search for one subject "
            "across lives. mention_count is how many segments mention an instrument — airtime, "
            "not importance — and counts are NOT comparable between lives: check "
            "mention_index_scope, which is equities_only for MB20261028 and multi_asset for "
            "MB20261029, so the same request returns different classes of instrument per live."
        ),
    )
    @_clear_errors
    def instruments_resource(live_id: str):
        return instruments_view(store, live_id)

    @server.resource(
        "easygoing://speakers/{speaker_id}",
        name="speaker",
        title="Speaker",
        mime_type="application/json",
        description=(
            "A speaker's registry entry and the lives they appear in, with per-live segment "
            "counts and the chapters they headed. Only registry fields are returned: role, "
            "affiliation, links and disclosures are curated by hand and are null or empty until "
            "someone fills them in — read curation_note before concluding anything from an empty "
            "field, and never infer a speaker's role or affiliation from what they said."
        ),
    )
    @_clear_errors
    def speaker_resource(speaker_id: str):
        return speaker_view(store, speaker_id)

    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True)

    @server.tool(
        name="find_mentions",
        title="Find mentions of a subject",
        annotations=read_only,
        description=(
            "Find where a subject is discussed, with timestamps and speakers. Omit live_id to "
            "search every live; give it to search one. Optional speaker (id, full name or "
            "surname) and date_from/date_to (ISO dates) narrow the search. Results are grouped "
            "by live, most recent first, and ordered by start time within a live.\n\n"
            "Use this to locate a subject. Use get_subject_discussion instead when you want what "
            "each speaker actually said about it in one live, with surrounding context.\n\n"
            "A subject is matched against the instrument registry first, then against transcript "
            "text; subject.match_kind says which, and subject.variants_used lists any other "
            "wording searched for the same thing. Results are paginated: pass next_cursor back "
            "as cursor. Read each group's caveats — the two lives differ in whether timestamps "
            "are estimated and in what their mention index covers. Text matching works on whole "
            "words within one segment, so a word the transcription split across two segments "
            "(rare) is not found."
        ),
    )
    @_clear_tool_errors
    def find_mentions_tool(
        subject: str,
        live_id: str | None = None,
        speaker: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> FindMentionsResult:
        return find_mentions(
            store, settings, subject, live_id, speaker, date_from, date_to, limit, cursor
        )

    @server.tool(
        name="get_speaker_coverage",
        title="What a speaker covered in a live",
        annotations=read_only,
        description=(
            "List the instruments and raw topics one speaker covered in one live. Instruments "
            "come from mentions falling inside that speaker's own segments; topics are the "
            "verbatim topic text of the chapters they headed, not a taxonomy.\n\n"
            "Use this for a speaker-shaped question. Use the instruments resource for the whole "
            "live, or find_mentions to follow one subject across lives.\n\n"
            "An empty instruments list does not mean the speaker discussed nothing: check "
            "mention_index_scope. MB20261028 indexes single equities only, so a block about oil, "
            "gold or currencies yields no instruments there while topics_raw still names them."
        ),
    )
    @_clear_tool_errors
    def get_speaker_coverage_tool(live_id: str, speaker: str) -> SpeakerCoverageResult:
        return speaker_coverage(store, live_id, speaker)

    @server.tool(
        name="rank_subjects",
        title="Rank subjects by mentions",
        annotations=read_only,
        description=(
            "Rank instruments by how many segments mention them over a date range. kind must be "
            "'instrument': topics cannot be ranked because there is no topics registry, and the "
            "tool raises rather than invent categories.\n\n"
            "The measure is mention_count — airtime, not importance, and not a measure of "
            "conviction or of how positively something was discussed. Check comparable: when "
            "false, the lives in range index mentions at different scopes, so a subject that "
            "could only be counted in one live will rank below one counted in both. Use "
            "find_mentions to see the actual segments behind any count."
        ),
    )
    @_clear_tool_errors
    def rank_subjects_tool(
        date_from: str | None = None,
        date_to: str | None = None,
        kind: str = "instrument",
        limit: int | None = None,
    ) -> RankSubjectsResult:
        return rank_subjects(
            store, date_from, date_to, kind, limit if limit is not None else settings.default_limit
        )

    # Section 6: advice-risk primitives are not registered at all unless enabled.
    if settings.enable_advice_risk:
        @server.resource(
            "easygoing://lives/{live_id}/summary",
            name="live_summary",
            title="Summary of a live",
            mime_type="text/markdown",
            description=(
                "A pre-generated, compliance-reviewed Markdown summary of one live, where every "
                "opinion is attributed to its speaker. The server never generates a summary: if "
                "no reviewed file exists for the live, this returns an error saying so. Prefer "
                "the chapters and instruments resources when you need structure rather than prose."
            ),
        )
        @_clear_errors
        def summary_resource(live_id: str) -> str:
            # The disclaimer is part of every advice-risk response (section 6).
            return f"{SUMMARY_HEADER}\n\n{read_summary(settings, live_id, store)}"

        @server.tool(
            name="get_subject_discussion",
            title="What each speaker said about a subject",
            annotations=read_only,
            description=(
                "Report what each speaker said about a subject in one live, as attributed quotes "
                "with timestamps. Grouped by speaker, speakers ordered by when they first raise "
                "the subject, each group in broadcast order and expanded with adjacent segments "
                "of the same speaker in the same chapter so a quote is not cut mid-argument; "
                "matched_segment_ids says which segments actually matched.\n\n"
                "Use this when the question is what was said. Use find_mentions when the question "
                "is where or whether something came up.\n\n"
                "Everything returned is a quotation, never a paraphrase, and an opinion belongs "
                "to the speaker it is attributed to — do not restate one as fact. Numbers inside "
                "the text come from automatic transcription and are unverified. Check "
                "any_needs_audio_review: where true, the speaker label itself is unconfirmed."
            ),
        )
        @_clear_tool_errors
        def get_subject_discussion_tool(
            live_id: str, subject: str, speaker: str | None = None
        ) -> SubjectDiscussionResult:
            return get_subject_discussion(
                store, settings, live_id, subject, speaker, disclaimer=DISCLAIMER
            )

        @server.tool(
            name="list_rubric_occurrences",
            title="Occurrences of a recurring rubric",
            annotations=read_only,
            description=(
                "List every occurrence of a recurring rubric across lives, most recent first, "
                "with the start time, the speakers heard in that chapter and the instruments "
                "mentioned in it. Accepts a rubric id, name or alias; date_from/date_to are ISO "
                "dates.\n\n"
                "Use this to follow a recurring segment such as the chart of the day over time. "
                "Use the chapters resource for the running order of a single live.\n\n"
                "Read start_quality on each occurrence: a published chapter time flagged "
                "unverified may disagree with the transcript, by about five minutes in one "
                "MB20261029 chapter."
            ),
        )
        @_clear_tool_errors
        def list_rubric_occurrences_tool(
            rubric: str, date_from: str | None = None, date_to: str | None = None
        ) -> RubricOccurrencesResult:
            return rubric_occurrences(store, rubric, date_from, date_to, disclaimer=DISCLAIMER)

        @server.prompt(
            name="summarize_live_portion",
            title="Summarize part of a live",
            description=(
                "Build a prompt to summarize a portion of one live, selected by chapter, speaker "
                "and/or time window (at least one is required; they combine as an intersection). "
                "The prompt embeds the selected segments verbatim with timestamps, speakers and "
                "audio-review flags, plus fixed rules: attribute every opinion to its speaker, "
                "flag unconfirmed attributions, keep transcribed numbers as unverified, add no "
                "outside facts, and say when the selection is capped or reaches a truncated end. "
                "Examples: \"Riassumi l'intervento di Michettoni\" (speaker), \"Cosa è stato "
                "detto nello Speciale 0DTE?\" (chapter), \"dal minuto 40 al minuto 55\" "
                "(start_s=2400, end_s=3300). For a whole live use the summary resource instead."
            ),
        )
        @_clear_prompt_errors
        def summarize_live_portion_prompt(
            live_id: Annotated[str, Field(description="Identifier of the live, e.g. MB20261028.")],
            chapter: Annotated[
                str | None,
                Field(
                    description=(
                        "Chapter label or rubric name (e.g. 'Grafico del giorno', 'Speciale "
                        "0DTE', a guest's name), a chapter_id, or a chapter number."
                    )
                ),
            ] = None,
            speaker: Annotated[
                str | None, Field(description="Speaker id, full name or surname.")
            ] = None,
            start_s: Annotated[
                str | None,
                Field(description="Window start: seconds from broadcast start, or mm:ss / h:mm:ss."),
            ] = None,
            end_s: Annotated[
                str | None,
                Field(description="Window end: seconds from broadcast start, or mm:ss / h:mm:ss."),
            ] = None,
        ) -> str:
            selection = select_portion(store, settings, live_id, chapter, speaker, start_s, end_s)
            return render_summarize_prompt(selection)

        @server.prompt(
            name="compare_views",
            title="Compare speakers' views on a subject",
            description=(
                "Build a prompt comparing what different speakers said about one subject within "
                "one live, highlighting agreement and disagreement. Embeds the output of "
                "get_subject_discussion (matching segments plus same-speaker context, tagged "
                "[match]/[context]) with fixed rules: attributed quotes only, no inferred "
                "positions, no outside facts, unverified numbers flagged. speakers is an optional "
                "comma-separated list; omit it to compare everyone who discussed the subject. "
                "Single live only. Examples: \"Gli ospiti di oggi erano d'accordo sulla Fed?\", "
                "\"Michettoni e Prisco vedono il Nasdaq allo stesso modo?\"."
            ),
        )
        @_clear_prompt_errors
        def compare_views_prompt(
            live_id: Annotated[str, Field(description="Identifier of the live, e.g. MB20261029.")],
            subject: Annotated[
                str,
                Field(description="Instrument (name or ticker), asset class or macro theme."),
            ],
            speakers: Annotated[
                str | None,
                Field(
                    description=(
                        "Comma-separated speakers to compare (ids, names or surnames); all who "
                        "discussed the subject if omitted."
                    )
                ),
            ] = None,
        ) -> str:
            material = comparison_material(
                store, settings, live_id, subject, speakers, disclaimer=DISCLAIMER
            )
            return render_compare_prompt(material)

        logger.info("advice-risk primitives registered (EASYGOING_ENABLE_ADVICE_RISK is on)")
    else:
        logger.info("advice-risk primitives NOT registered (EASYGOING_ENABLE_ADVICE_RISK is off)")

    return server


def main() -> None:
    """Run over stdio. Logs go to stderr; stdout carries the protocol."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
