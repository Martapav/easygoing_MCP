# CLAUDE.md — EasyGoing MCP server, transcript-only prototype

## 1. Goal and boundaries

Build a working, local MCP server that exposes the content of EasyGoing live broadcasts (canonical transcript JSONs) to MCP hosts (Claude Desktop, Claude Code, MCP Inspector).

This is a prototype on local files. It must be structured so that the storage backend can later be swapped (PostgreSQL inside the EasyGoing app) without changing the MCP surface.

In scope: everything computable from the canonical transcripts and the registries alone.
Out of scope (do NOT implement, do NOT stub with fake data): anything that depends on LLM-extracted derived tables (stances, price levels, figures, events, disclosed positions), on external data (market prices, fundamentals, calendars, glossary), on authentication, or on remote transport.

The request catalog (`data/catalog.json`) is the specification. Tool names, resource URI templates, prompt names and parameter names come from it verbatim. Do not rename them.

## 2. Repository layout (create it if missing)

```
easygoing-mcp/
  CLAUDE.md
  pyproject.toml
  data/
    catalog.json                       # request catalog (spec). READ-ONLY.
    source/                            # raw transcripts as delivered. READ-ONLY, never edit.
      Market_Briefing_2026-10-28_MCP_TEST.json
      Market_Briefing_2026-10-29_MCP_TEST.json
      Market_Briefing_2026-10-2{8,9}_TRASCRIZIONE_CORRETTA.txt   # standard .txt route input
    canonical/<live_id>.canonical.json # BUILD OUTPUT of scripts/ingest.py. Never hand-edit.
    canonical_txt/<live_id>.canonical.json  # BUILD OUTPUT of scripts/ingest.py --route txt
    registries/                        # seeded by ingest, then curated by humans (see 4.4)
      speakers.json  rubrics.json  instruments.json  programmes.json
    derived/summaries/<live_id>.md     # optional, produced outside this repo
  docs/
    transcript_txt_format.md   # the .txt input spec (also the instructions for transcript authors)
  scripts/
    ingest.py          # source -> canonical + registry seeding (--route json|txt)
    validate_data.py   # integrity report on canonical + registries (--canonical-source json|txt)
    diff_canonical.py  # field-level diff, data/canonical vs data/canonical_txt
  src/easygoing_mcp/
    server.py        # FastMCP wiring only: decorators, no business logic
    config.py        # paths, feature flags, limits (env vars with defaults)
    ingest/
      detect.py              # source format detection
      mb_mymeeting_en_v0.py  # adapter for the 2026-10-28 format
      mb_mymeeting_it_v1.py  # adapter for the 2026-10-29 format
      txt_parse.py  txt_metadata.py  transcript_txt_v1.py   # the standard .txt route (4.7)
      timecodes.py  slugs.py
    models/
      source_en_v0.py  source_it_v1.py   # Pydantic models of the RAW formats
      source_txt_v1.py                   # Pydantic models of a PARSED .txt
      canonical.py                       # Pydantic models of canonical files
      registries.py
      outputs.py                         # tool/resource OUTPUT models
    store/
      base.py        # ContentStore Protocol (the swap point)
      json_store.py  # loads data/canonical + data/registries at startup
    services/        # pure functions over ContentStore; no MCP imports here
      lives.py  chapters.py  mentions.py  search.py  speakers.py  rubrics.py
      subjects.py    # subject string -> instrument_id resolution
    prompts/
      templates.py
  tests/
    fixtures/        # small hand-made source and canonical files
    test_ingest.py  test_models.py  test_services.py  test_server.py  test_acceptance.py
```

Rules: the server reads only `data/canonical/` and `data/registries/`, never `data/source/`. `services/` must be testable without MCP. `server.py` only translates between MCP and services. Adding a new live means dropping a source file and re-running ingest; if its format is unknown, `detect.py` must fail with a clear error (no best-effort parsing).

## 3. Stack and grounding rules

- Python >= 3.11, dependency management with `uv`.
- Official SDK only: `uv add "mcp[cli]"` (package `mcp`, class `MCPServer` from `mcp.server.mcpserver`). Do NOT use the separate third-party `fastmcp` package; its API differs.
- SDK major version: this project targets **mcp 2.x**. `FastMCP` was renamed to `MCPServer` in mcp 2.0; `mcp.server.fastmcp` is now a tombstone module that raises `ModuleNotFoundError` on import. Earlier revisions of this file named the 1.x path; that was corrected against the installed source on 2026-11-02. Python-side annotation fields are snake_case (`read_only_hint`) and serialize to the wire names (`readOnlyHint`).
- Pin the installed `mcp` version in `pyproject.toml` and record it in section 11 of this file.
- Before using any SDK feature (structured output, tool annotations, prompt message types, in-memory test client), verify it exists in the installed version by reading the installed package source under `.venv/lib/python*/site-packages/mcp/` or the SDK README. Do not rely on memory of the API. If a feature is missing, report it and propose the fallback; do not invent an API.
- Verified behaviour (2026-11-02): section 5 says FastMCP turns tool exceptions into `isError`
  results. In mcp 2.3.0 that holds **only** for `ToolError` and `ResourceError`, whose text is
  preserved after an `Error executing tool <name>:` prefix. Every other exception becomes
  `UnexpectedToolError("Error executing tool <name>")` and, per the SDK's own docstring,
  "nothing from an unexpected exception reaches the client". `server.py` therefore re-raises
  anticipated failures (bad id, ambiguous or unresolvable subject, foreign cursor, unavailable
  ranking kind) as `ToolError`, and leaves genuine crashes alone so a bug still reads as a bug.
- Verified behaviour (2026-11-02): a resource or resource-template handler that raises anything
  other than `ResourceError` or `MCPError` has its exception replaced by the SDK with
  `UnexpectedResourceError("Error creating resource from template ...")`, discarding the message.
  Section 5 requires the message to carry the valid options, so `server.py` translates store
  lookup failures into `ResourceNotFoundError` (`-32602`), which the SDK passes through intact.
  SDK result models use snake_case attributes in Python (`resource_templates`, `uri_template`,
  `mime_type`) even though the wire format is camelCase.
- Verified present in the pinned version (2026-11-02): `MCPServer` with `.tool()`/`.resource()`/`.prompt()` decorators and `.run(transport="stdio")`; structured output via `tools/base.py:output_schema` and `.tool(structured_output=...)`; `ToolAnnotations.read_only_hint`; prompt returns accepting `str`/`Message`/dict/sequence; in-memory testing via `mcp.Client(server)` (accepts an `MCPServer` in-process) and `mcp.shared.memory.create_client_server_memory_streams()`; the `mcp dev` Inspector command.
- Pydantic v2 for all models.
- Reference docs: MCP spec at modelcontextprotocol.io/specification (pages server/resources, server/tools, server/prompts); SDK at github.com/modelcontextprotocol/python-sdk; Inspector at github.com/modelcontextprotocol/inspector.

stdio transport rule: the server communicates over stdout. Nothing else may write to stdout. Use `logging` configured to stderr; never `print()`.

## 4. Milestone 0 — ingestion: source files to canonical files

The two lives arrive in two different raw formats. The MCP layer must never see that difference. Build two adapters that produce one canonical format, validate it, and report before Milestone 1.

### 4.1 Canonical schema (target)

```
live:      live_id, date (ISO), title|null, programme_id, video_url|null,
           source_format, source_file, duration_seconds|null
coverage:  content_end_s, truncated (bool), mention_index_scope ("equities_only"|"multi_asset"),
           timestamps_kind ("asr_chunk_start"|"estimated_interpolated")
chapters[]: chapter_id ("<live_id>-C<NN>"), start_s, start_quality, label_raw,
            rubric_id|null, speaker_ids[], topics_raw|null
segments[]: segment_id, start_s, end_s, start_quality, chapter_id, text,
            speaker {speaker_id, confidence, needs_audio_review},
            mixed_speakers (bool|null), source {...original per-segment fields, untouched}
mentions[]: instrument_id, segment_id, origin ("source_index")
anomalies[]: kind, segment_ids[]|chapter_id|null, detail
```

Conventions that apply to both adapters:
- `end_s` is the next segment's `start_s`. For the last segment it is `coverage.content_end_s`. This yields zero-duration segments: 25 in MB20261028 and 31 in MB20261029, which must be reproduced exactly, since the catalog reports those counts. Each one gets an anomaly of kind `zero_duration`.
- `confidence` keeps the source vocabulary (no cross-format mapping onto a common scale): `contextual`/`review_audio` for en_v0, `alta`/`media` for it_v1. Downstream code uses `needs_audio_review` for logic; `confidence` is only displayed.
- `mixed_speakers` is `null` unless the source marks it per segment. Neither source does, so it is `null` everywhere. Do not infer it from text.
- Every source field not mapped into canonical fields is preserved in `segments[].source` (or `live.source_meta`). Nothing is dropped silently.
- Speaker and rubric names are slugged to ids (`davide-biocchi`, `grafico-del-giorno`). Slugging normalizes typographic apostrophes (’ to ') and strips accents before slugging, so `Chiediamolo all’AI` and `Chiediamolo all'AI` map to the same id.

### 4.2 Adapter `mb_mymeeting_en_v0` (Market_Briefing_2026-10-28_MCP_TEST.json)

Detection: top-level keys `metadata`, `securities_index`, `segments`, `search_chunks`.

Live-level fields:
- `live_id`: prefix of `segments[].id` (`MB20261028`). Assert all segments share it.
- `date`: parse from `metadata.source` (free text, `28 Oct 2026`). Cross-check against the date in the file name; fail on mismatch.
- `title`: `null`. The source has none. Do NOT use the programme name as title.
- `programme_id`: from the file name prefix (`Market_Briefing`), giving `market-briefing`.
- `video_url`: `metadata.video`.
- `coverage.content_end_s`: parse `metadata.broadcast_content_until_youtube_approx` (expected 5941). `truncated`: false. `mention_index_scope`: `equities_only`. `timestamps_kind`: `asr_chunk_start`.

Segments:
- `segment_id`: `id` unchanged.
- `start_s`: parse `timestamp_youtube`. The format is `mm:ss` below one hour and `h:mm:ss` above; write one parser and test both forms.
- `start_quality`: from `timecode_kind`. `raw_asr_chunk_start` becomes `asr_chunk_start`; `official_chapter` becomes `chapter_official`.
- `speaker.confidence`: `confidence`. `needs_audio_review`: `confidence == "review_audio"` (expected 72).
- `source`: `timestamp_original`, `timecode_kind`, `source_line`, `speaker_correction`.

Chapters:
- Source is `metadata.chapters[]`, which has `youtube` and `label`.
- The label is `Left — Right` or a bare name. If `Left` is a known speaker (appears in `segments[].speaker`), the chapter is a guest block: `speaker_ids=[Left]`, `rubric_id=null`, `topics_raw=Right`. Otherwise `Left` is a rubric: `rubric_id=slug(Left)`, `topics_raw=Right` or null.
- `start_quality`: `chapter_official`.
- Segment-to-chapter assignment is time-based: the last chapter whose `start_s <= segment.start_s`.

Mentions:
- One row per (`securities_index[].name`, segment id in `securities_index[].segments`), with `origin="source_index"`.
- `first_mention` and `speakers` from the index are not stored; they are recomputed from mentions and segments, with a check that they agree.

Other:
- `search_chunks` are ignored. They are a derived retrieval chunking with inherited timecodes, and search runs on segments. Record this in the ingest report.
- `metadata.disclaimer`, `timecode_precision` and `security` go into `live.source_meta`.

### 4.3 Adapter `mb_mymeeting_it_v1` (Market_Briefing_2026-10-29_MCP_TEST.json)

Detection: `schema == "easygoing.market_briefing.transcript_test.v1"`.

Live-level fields:
- `live_id`: `"MB" + data` without dashes (`MB20261029`), because the source has no id.
- `date`: `data`. `title`: `titolo`. `programme_id`: slug(`programma`). `video_url`: `null`.
- `coverage.truncated`: `contenuto_incompleto` (true). `content_end_s`: last segment's `time_youtube_estimated_seconds` (expected 5481). `duration_seconds`: `null`. `mention_index_scope`: `multi_asset`. `timestamps_kind`: `estimated_interpolated`.

Segments:
- `segment_id`: `f"{live_id}-{id:03d}"`. Source ids are 0..240 integers.
- `start_s`: `time_youtube_estimated_seconds`. `start_quality`: `estimated_interpolated`.
- `speaker`: `speaker`. `confidence`: `speaker_confidence`. `needs_audio_review`: `requires_audio_check` (expected 52).
- `source`: `time_recording`, `time_recording_seconds`, `speaker_originale`, `rubrica`.

Chapters:
- Source is `capitoli_ufficiali[]`. `start_s` comes from `timestamp_youtube_ufficiale` after stripping annotations such as `[da verificare]`.
- `start_quality`: `chapter_official`, or `chapter_official_unverified` when an annotation was stripped.
- If `nome` is in `speaker_ufficiali`, it is a guest block (`speaker_ids=[nome]`, `rubric_id=null`); otherwise `rubric_id=slug(nome)`. `topics_raw`: `tema`.

Segment-to-chapter assignment uses the per-segment `rubrica` field matched to the chapter by name, not time. Reason: segment times are interpolated and one chapter time is known to be wrong by about 5 minutes. For each segment where `rubrica` and time-based assignment disagree (expected 19), add an anomaly of kind `chapter_assignment_conflict` carrying both candidates. If the user later prefers time-based assignment, it is a one-line switch in `config.py`. Implement it that way.

Mentions:
- One row per (`titoli_e_strumenti[].nome`, each id in `segmenti_ids`, mapped to the canonical segment id), with `origin="source_index"`.
- The per-speaker counts in `speaker` are recomputed and checked, not stored.

Other:
- `limiti`, `origine`, `tempo_youtube_segmenti` and the recording-time fields go into `live.source_meta`.
- Each item in `limiti` also becomes a live-level anomaly of kind `source_limitation`.

### 4.4 Registry seeding

No registries exist yet. `ingest.py` creates or merges them. It never overwrites existing fields, only adds new ids and new aliases, so human curation survives re-ingestion.
- `speakers.json`: `{speaker_id, name, aliases[], role: null, affiliation: null, links: [], disclosures: []}` from all speaker names. Do not fill role or affiliation from the transcripts; those come from the user.
- `rubrics.json`: `{rubric_id, name, aliases[]}` from chapter labels classified as rubrics.
- `instruments.json`: `{instrument_id, name, aliases[], tickers[], asset_class: null}`. Merge across lives by normalized name; record each ticker as given (e.g. `GOOGL / GOOG` and `GOOGL/GOOG` both normalize to `[GOOGL, GOOG]`). When names match but tickers conflict, or a ticker is missing in one live (SK hynix), keep both values and flag them in the ingest report. Do not resolve conflicts automatically. `asset_class` stays null; do not guess.
- `programmes.json`: `{programme_id, name}`.
- There is no topics registry. Topic subjects are resolved by text match only (section 7, subject resolution).

### 4.5 Validation and report

`scripts/validate_data.py` checks:
- referential integrity (segment to chapter, mention to segment, every id to its registry);
- monotonic `start_s`;
- `end_s >= start_s`;
- the expected counts above: segments 137 and 241; zero-duration 25 and 31; audio review 72 and 52; chapter conflicts 0 and 19.

It prints a report. Any count mismatch fails the run.

If canonical files produced elsewhere (e.g. in the catalog project) are provided, diff them against this ingest output and report differences. Do not prefer either silently.

### 4.6 Known data facts the server must respect

- Speakers are context-inferred in both lives, not audio-verified.
- MB20261029 timestamps are interpolated estimates; it has no video URL (no deep links) and is truncated mid-sentence.
- Mention scope differs (equities only vs multi-asset). Mention counts are NOT comparable across these lives.
- Spoken numbers are ASR-garbled and unverified (both sources say so).

### 4.7 Standard .txt route `transcript_txt_v1` (added 2026-11-03)

A second ingest route, alongside the JSON adapters, which stay as they are. Input is a plain
`.txt` transcript in `data/source/`. The grammar is specified in `docs/transcript_txt_format.md`,
which is the contract and also the instructions for whoever writes future transcripts. Output
uses the same canonical schema and goes to `data/canonical_txt/`.
`uv run python scripts/ingest.py --route txt [--dry-run]`.

- **Parser** (`ingest/txt_parse.py`): one grammar that accepts both observed variants: bare
  `[mm:ss]` or labelled `[REC … | YT~ …]` clocks, column or pipe chapter lists, and the
  `prima occorrenza` or `N segmenti` index forms. Any line that matches no rule fails with
  `file:line`. `detect.py:detect_source` claims a `.txt` only if it has a title line and a
  segment line.
- **Metadata** (`ingest/txt_metadata.py`): resolved from the `---` block, then from line forms
  (`Video:`, `OSPITI…:`, a `~` on the YT clock), then — **temporarily** — from the companion
  JSON source with the same programme and date, run through its existing adapter. Every
  borrowed field is listed in `live.source_meta.metadata_fallback` and in the ingest report.
  The user decided this on 2026-11-03: a colleague will produce .txt files with a complete
  metadata block. A required field with no source fails.
- **Adapter** (`ingest/transcript_txt_v1.py`):
  - live id = programme initials + date; segment ids `<live_id>-001…` in file order;
  - `==== SECTION ====` headers decide chapter assignment when present (n-th header of a name →
    n-th chapter of that name), with conflicts recorded; otherwise assignment is by time. The
    switch is `EASYGOING_CHAPTER_ASSIGNMENT_TXT_V1`;
  - a bracketed flag before the speaker's colon sets `needs_audio_review`; `confidence` is the
    flag text, or `unflagged`;
  - `NOTE…` bullets become `source_limitation` anomalies.
- **Mentions**: the .txt index lists no segments. Each entry's name, each part of a spaced
  `A / B` name, curated registry aliases, and tickers (case-sensitive, whole token) are matched
  on token boundaries. Matches get the new `MentionOrigin` `index_text_match`. The index's stated
  first occurrence, count and speakers are cross-checked and flagged, never corrected.
- **Server switch**: `EASYGOING_CANONICAL_SOURCE=json|txt` (default `json`) picks
  `data/canonical` or `data/canonical_txt`. The new `SourceFormat` is `transcript_txt_v1`.

## 5. Output contract (applies to every tool and resource)

Every item derived from a segment returns a `SegmentRef`:

```
live_id, segment_id, start_s, end_s, start_quality,
timestamp_label (hh:mm:ss, prefixed with "~" when start_quality is not exact),
speaker_id, speaker_name, attribution_confidence, needs_audio_review, mixed_speakers,
text, deep_link (null when live.video_url is absent)
```

Every response that covers a live also carries a `LiveCaveats` block built from data, never hardcoded:
- `truncated`
- `mention_index_scope`
- `timestamps_estimated`
- anomalies overlapping the returned segments

Rules:
- Opinions are returned as attributed quotes from segments. No tool paraphrases, aggregates or restates a speaker's view as fact.
- Numbers inside quoted text are never extracted, normalized or "corrected".
- Deep links: build `deep_link` from `video_url` plus the integer `start_s` only when `video_url` exists. Mark it approximate when timestamps are estimated.
- Tools return Pydantic models so the SDK emits `structuredContent` and `outputSchema` (verify per section 3). Text content must be a faithful rendering of the same data.
- Lists are bounded: `limit` defaults to 10, with a hard max of 50 in `config.py`. Return `next_cursor` (opaque string) when more results exist. Never return an unbounded segment list.
- Errors: unknown `live_id`, `speaker_id`, rubric or unresolvable subject raise a clear exception. Its message must list valid options (or the closest matches) so the model can retry. FastMCP turns tool exceptions into `isError` results; verify this in the installed version.

## 6. Advice-risk gating

Catalog entries with `advice_risk: true` are not cleared for exposure yet; a policy decision is pending. Implement a flag `EASYGOING_ENABLE_ADVICE_RISK` (default `false`).
- When false, these primitives are not registered at all. Hiding them in descriptions is not enough.
- When true (local dev and tests), every response from them includes a fixed `disclaimer` field. The text lives in `prompts/templates.py` and contains no investment advice.

The tests run both configurations.

## 7. What to implement (Phase 1)

Implement in this order. Each item names the catalog entry it satisfies.

### Resources

| Catalog | URI | Content |
|---|---|---|
| REQ-0021 | `easygoing://lives` | Index of lives, most recent first: live_id, date, title, programme, guest speaker names, chapter count, truncated. |
| REQ-0002 | `easygoing://lives/{live_id}/chapters` | Chapters with start time, start_quality, rubric name, speaker names, topics_raw. |
| REQ-0003 | `easygoing://lives/{live_id}/instruments` | Instruments from `mentions[]`: speakers who discussed each, first mention (SegmentRef), mention count, plus `mention_index_scope`. |
| REQ-0011 | `easygoing://speakers/{speaker_id}` | Speaker registry entry and the lives they appeared in. Only registry fields; role, affiliation and disclosures are null until curated, and the output says so. |
| REQ-0001 | `easygoing://lives/{live_id}/summary` | advice_risk. Serves `data/derived/summaries/<live_id>.md` if it exists; otherwise a clear "not yet generated" error. NEVER generate a summary inside the server. |

Resources return JSON (`mimeType: application/json`), except the summary, which is Markdown.

### Tools (all read-only; set `readOnlyHint=True` annotations if supported)

| Catalog | Name | Parameters | Behaviour |
|---|---|---|---|
| REQ-0005, REQ-0006 | `find_mentions` | `subject` (req), `live_id`, `speaker`, `date_from`, `date_to`, `limit`, `cursor` | One tool for both entries: `live_id` omitted means cross-live search. Returns SegmentRefs where the subject occurs, grouped by live, ordered by date desc then start_s. `period` from the catalog is split into `date_from`/`date_to` (ISO dates) because tool params should be flat. |
| REQ-0007 | `get_subject_discussion` | `live_id` (req), `subject` (req), `speaker` | advice_risk. Same retrieval as `find_mentions`, grouped by speaker, each group ordered by time, with contiguous context: the matching segment plus adjacent segments of the same speaker in the same chapter, capped. |
| REQ-0010 | `get_speaker_coverage` | `live_id` (req), `speaker` (req) | Instruments (from mentions in the speaker's segments) and raw topics (`topics_raw` of chapters listing the speaker), with `mention_index_scope`. |
| REQ-0015 | `rank_subjects` | `date_from`, `date_to`, `kind` (`instrument` only in Phase 1; `topic` raises until a topics registry exists), `limit` | Mention counts per subject. Output is labelled `mention_count` (airtime, not importance) and carries a `comparable: false` warning whenever the lives in range have different `mention_index_scope`. |
| REQ-0017 | `list_rubric_occurrences` | `rubric` (req), `date_from`, `date_to` | advice_risk. Chapters with that rubric across lives: live, start, speakers, instruments mentioned in the chapter's segments. |

### Prompts

MCP prompt arguments are strings. Parse and validate numeric arguments inside the prompt function.

| Catalog | Name | Arguments | Behaviour |
|---|---|---|---|
| REQ-0004 | `summarize_live_portion` | `live_id` (req), `chapter`, `speaker`, `start_s`, `end_s` | advice_risk. At least one selector is required; raise otherwise. Embeds the selected segments (as SegmentRef-rendered lines) and fixed instructions: attribute every opinion to its speaker; flag segments with `needs_audio_review`; state if the selection hits a truncated end; do not add facts. Cap embedded text (config). |
| REQ-0013 | `compare_views` | `live_id` (req), `subject` (req), `speakers` (comma-separated) | advice_risk. Embeds the output of `get_subject_discussion` and fixed instructions for comparing views: agreement/disagreement, attributed quotes only. Single-live only in Phase 1 (REQ-0016 needs the stance table). |

### Subject resolution (`services/subjects.py`)

This is used by every tool that takes `subject`.
1. Normalize: casefold, strip accents (`unicodedata` NFKD), unify apostrophes, collapse whitespace.
2. Match the normalized subject against instrument registry names, aliases and tickers. On a match, use `mentions[]` plus a text match on the segment for the name and aliases.
3. No registry match: fall back to normalized substring/token match over `segments[].text`. Mark the result with `match_kind: "text"`, as opposed to `"registry"`.
4. Ambiguous registry match: return an error listing candidates. Do not guess.

Keep the search behind `ContentStore.search_segments(...)`, so that Postgres full-text search (Italian config) or pgvector can replace it later. Do not add embeddings or a vector DB in Phase 1. With two lives, a linear scan is correct and fast enough.

## 8. Explicitly deferred (do not implement, do not stub)

REQ-0008 `get_cited_levels`, REQ-0009 `get_reported_figures`, REQ-0012 `get_mentioned_events`, REQ-0014 `get_stance_history`, REQ-0016 (cross-live `compare_views`), REQ-0018 `get_directional_calls`, REQ-0019 `compare_calls_with_prices`, REQ-0020 `explain_term`, REQ-0022 `get_disclosed_positions`.

Reason: each depends on a derived extraction table or external data that does not exist yet. If asked to "make them work quickly", refuse to fabricate the data and point to this section.

## 9. Testing

- Ingest tests: both adapters on small source fixtures. These must cover: the `mm:ss`/`h:mm:ss` parser; chapter label classification (guest vs rubric); the `[da verificare]` annotation; apostrophe normalization; registry merge preserving curated fields; unknown format (must fail).
- Unit tests on `services/` with small canonical fixtures. These must cover: zero-duration segments, missing `video_url`, truncated live, `mixed_speakers` null, unknown enum value (must fail).
- Protocol tests: list resources, templates, tools and prompts, and call each one, through an in-memory client session if the installed SDK provides one (check `mcp.shared.memory`); otherwise spawn the server over stdio with `mcp.client.stdio`. Assert that `structuredContent` validates against `outputSchema`.
- Acceptance tests (`test_acceptance.py`) against the real canonical files, derived from catalog `evidence`:
  - `find_mentions(subject="Stellantis")` returns hits in both MB20261028 and MB20261029, including a hit overlapping 1496–1651 s in MB20261028 and 2605–2722 s in MB20261029.
  - `get_subject_discussion(live_id="MB20261028", subject="Fed")` returns at least two distinct speakers with segments overlapping 2615–2990 s or 3476–3581 s.
  - `easygoing://lives/MB20261029/chapters` includes the rubrics announced around 707–922 s.
  - `rank_subjects` over both lives returns `comparable: false`.
  - `list_rubric_occurrences(rubric="Grafico del giorno")` returns one occurrence per live.
  - `summarize_live_portion(live_id="MB20261028")` with no selector raises.
  - With `EASYGOING_ENABLE_ADVICE_RISK=false`, advice-risk primitives are absent from the list results.

  If an acceptance test fails because the data differs from the catalog note, report the discrepancy. Do not weaken the assertion silently.
- Manual: `uv run mcp dev src/easygoing_mcp/server.py` (Inspector UI) and the Inspector CLI mode for scripted checks.

## 10. Running and registering

- Entry point: `uv run python -m easygoing_mcp.server` (stdio).
- Claude Code: `claude mcp add easygoing -- uv --directory <abs path> run python -m easygoing_mcp.server`.
- Claude Desktop: add the equivalent command to `claude_desktop_config.json`. Absolute paths only.
- Data directory configurable via `EASYGOING_DATA_DIR` (default `./data`).

## 11. Working rules for Claude Code

- Proceed milestone by milestone: 0 (ingestion + registries + validation), 1 (canonical models + store), 2 (resources), 3 (tools), 4 (prompts), 5 (tests + registration). Stop for review at the end of each milestone, with a short report: what was built, what deviated from this file, open questions.
- Never modify `data/source/` or `data/catalog.json`. Never hand-edit `data/canonical/`; change the adapter and re-ingest. Never invent fields, registry entries, speaker affiliations or numbers.
- Tool and resource descriptions are written for the model. State what the tool returns, when to use it versus siblings, and caveats (unverified numbers, inferred speakers). Use the catalog's `canonical_formulation` and Italian `example_phrasings_it` as the basis.
- Keep this file current. Update the status table below when a primitive lands, and record the pinned SDK version.

### Milestone status

- **M0 (ingestion, registries, validation): complete.** Both adapters build canonical files;
  `scripts/validate_data.py` reports 42/42 checks passing, including all section 4.5 counts.
  94 tests pass. Re-ingestion is byte-identical and preserves hand-curated registry fields.
- Deviation from section 2: added `src/easygoing_mcp/ingest/common.py` for the rules that must
  stay format-independent (end-time derivation, zero-duration anomalies, time-based chapter
  assignment), so the two adapters cannot drift apart on them.
- **M1 (canonical models + store): complete.** `models/registries.py`, `store/base.py`
  (`ContentStore` protocol) and `store/json_store.py` are in place; 129 tests pass.
- **M2 (resources): complete.** All five Phase-1 resources registered and tested through the
  SDK's in-memory client; 180 tests pass. Advice-risk gating verified in both configurations.
- **M3 (tools): complete.** All five Phase-1 tools registered, annotated `read_only_hint`, with
  catalog-verbatim parameter names; 246 tests pass. `structuredContent` is asserted to validate
  against each tool's `outputSchema`.
- **M4 (prompts): complete (2026-11-03).** Both prompts registered only under the advice-risk
  flag, catalog-verbatim argument names, one user message each ending with the disclaimer;
  314 tests pass (`tests/test_prompts.py`).
- Deviation from section 2: selection logic lives in a new `services/portions.py` (no MCP
  imports); `prompts/templates.py` holds the fixed instruction texts and the line rendering.
- Added to `config.py`: `max_prompt_chars` (default 40000, env `EASYGOING_MAX_PROMPT_CHARS`)
  alongside `max_prompt_segments`, because one MB20261028 segment is ~10,000 characters. Caps drop
  whole segments, never cut text, and the rendered prompt says `CAPPED` when they bite.
  `compare_views` drops context segments before matching ones and keeps every speaker represented.
- Verified behaviour (2026-11-03): `Prompt.render` re-raises any exception other than `MCPError`
  as `ValueError("Error rendering prompt <name>")`, discarding the message. `server.py` therefore
  raises anticipated prompt failures as `MCPError(INVALID_PARAMS)` (`-32602`), which reaches the
  client intact. Prompt arguments are declared `str` and parsed in `services/portions.py`.
- `summarize_live_portion` selector semantics: selectors intersect. `chapter` accepts a
  chapter_id, a chapter number, the full label, a rubric id/name/alias, or a contiguous run of
  label words ("Speciale 0DTE", a guest surname); several matches (Sartorelli heads two blocks in
  MB20261028) is an error listing them. `start_s`/`end_s` accept seconds or `mm:ss`/`h:mm:ss`; a
  segment already running at `start_s` is included. "Reaches a truncated end" means the selection
  (before capping) includes the live's last segment and the live is truncated.
- `compare_views` distinguishes requested speakers who speak in the live but never match the
  subject from those absent from the live altogether, and states both; it raises only when none
  of the requested speakers discussed the subject, listing who did.
- Fixed during M4: `server.py` registered the summary resource twice (once in each advice-risk
  block); the duplicate was removed.
- Decided 2026-11-03: a `summarize_live_portion` selection made only by speaker or time window may
  span most of a live. No hard window limit; the segment and character caps are the only brake,
  and the prompt states `CAPPED` when they apply.
- **M5 (acceptance + registration): see completion note below.** Step 1 done: `tests/test_acceptance.py` runs
  the seven section 9 checks through the in-memory client, all passing, plus stricter checks from
  the same catalog evidence (all four REQ-0007 Fed speakers in each live, Snam/F5 for Grafico del
  giorno, every announced MB20261029 rubric, no deep links for MB20261029). 327 pass, 1 xfail.
  Step 2 done: `tests/test_stdio.py` spawns `python -m easygoing_mcp.server` as a subprocess,
  exercises every primitive kind over real stdio, reads the advice-risk flag from the environment,
  and drives raw JSON-RPC to assert stdout carries nothing but protocol messages (logs on
  stderr). Launch via `uv --directory <abs path>` from `/` resolves `./data` correctly.
  330 pass, 1 xfail. Not done: the Inspector (`mcp dev`, Inspector CLI) needs Node/npx, which is
  not installed in this environment.
- **Accepted limitation (decided 2026-11-03).** REQ-0005 evidence lists four Stellantis windows
  in MB20261028; the server finds three. At 2530-2537 s the ASR chunking splits the word across
  segments (`Stellant` in MB20261028-027, `is` in -028), and the source `securities_index` lists
  neither. Matching across adjacent segment boundaries was rejected (false joins such as `F` +
  `ed`). Pinned by `test_word_split_across_segments_is_not_matched`; stated in the `find_mentions`
  description.
- Decided 2026-11-03: the MCP Inspector (`mcp dev`, Inspector CLI) is skipped for now; it needs
  Node/npx, not installed in this WSL environment. `tests/test_stdio.py` covers the stdio path.
- Registered 2026-11-03 with Claude Code (local scope, this project):
  `claude mcp add easygoing -e EASYGOING_DATA_DIR=/home/marta/MCP_server_exp/data --
  /home/marta/.local/bin/uv --directory /home/marta/MCP_server_exp run python -m
  easygoing_mcp.server`. `claude mcp get easygoing` reports Connected. Advice-risk flag off.
- Registered 2026-11-03 with Claude Desktop (Windows Store build, so the config is at
  `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`;
  backup alongside as `.bak-2026-11-03`). Desktop runs on Windows, so the entry launches
  `wsl.exe -d Ubuntu --cd <repo> -- env EASYGOING_DATA_DIR=<repo>/data <uv> --directory <repo> run
  python -m easygoing_mcp.server`; Windows env vars do not reach WSL, hence `env`. That command
  line was verified to answer `initialize`; the in-app connection awaits a Desktop restart.
- **M5: complete** apart from the in-app Desktop check and the Inspector (skipped, no Node).
  332 tests pass.
- Subject variants (decided 2026-11-02): a subject expands into the surface forms that denote the
  same thing — registry-derived (names, aliases, tickers, and each side of an `A / B` name) plus a
  short abbreviation table in `services/subjects.py`. `ResolvedSubject.variants_used` reports any
  form the user did not type, so the broadening is never silent. This resolves the `Fed` gap:
  all four speakers the catalog names for REQ-0007 are now found, Braganti included. FOMC is
  deliberately excluded — the committee is not the bank.
- Decision recorded for section 7: `SegmentQuery.instrument_ids` and `text_any` combine as a
  union, not an intersection, because section 7 resolves a subject with "`mentions[]` plus a text
  match". Every other filter ANDs. Segment text is matched on token boundaries, not as a bare
  substring, so `Oro` does not match `lavoro` (10 hits instead of 21 across the two lives).
- **Resolved (by the subject-variant decision above) — the `Fed` acceptance test.** Kept for the
  record; M5's acceptance test confirms it. Section 9 expects
  `get_subject_discussion(live_id="MB20261028", subject="Fed")` to return at least two distinct
  speakers overlapping 2615-2990 s or 3476-3581 s. Only Michettoni (2656-2990) uses the token
  `Fed`. Braganti's 3476-3530 segment, which the catalog's REQ-0007 evidence names, says
  "l'annuncio da parte della Federal Reserve" and never the abbreviation. With text-only subject
  resolution (section 7 step 3) and no topics registry (section 4.4), the assertion fails. It
  needs a decision: expand a subject into surface variants, curate an alias, or record the
  discrepancy. `SegmentQuery.text_any` is already a list so a resolver can pass several forms.
- Interpretation recorded for section 4.1: a `zero_duration` anomaly is emitted only for a
  segment whose *successor shares its start timecode*. MB20261029's final segment is also
  zero-length, because `content_end_s` is its own start, but that is the unknown broadcast end
  and is reported through `coverage.truncated` — counting it would give 32 where the
  specification and the catalog both say 31.

- **Standard .txt route (2026-11-03): built, awaiting review.** See section 4.7. On the two real
  lives it reproduces every section 4.5 count: segments 137/241, zero-duration 25/31, audio
  review 72/52, conflicts 0/19. Segment text, times, speakers, review flags and chapter
  assignment are identical to the JSON route. Coverage is identical too, but only because it is
  borrowed from the companion JSON. `validate_data.py --canonical-source txt` passes 38/38; the
  catalog's source_format/source_file are noted, not compared. The acceptance suite runs on both
  sources and passes on both. 403 tests pass.
  Known differences (`scripts/diff_canonical.py`):
  - mentions recall 85% (45/53) and 92% (102/111), precision 100%. The misses are spoken short
    forms ("Samsung", "Micron", "Dell", "Booking", "JPMorgan", "Brent"); curating registry
    aliases would close them;
  - 7 MB20261028 segments lose `chapter_official` start quality (the .txt has no such mark);
  - MB20261029 `label_raw` is the full chapter line;
  - MB20261029 has no `source_limitation` anomalies, because its .txt has prose, not a `NOTE`
    block; MB20261028 gains 7 from its `NOTE DI QUALITÀ` bullets;
  - `confidence` vocabulary and `source` keys differ.

| Catalog | Primitive | Status |
|---|---|---|
| REQ-0021 | resource `easygoing://lives` | done |
| REQ-0002 | resource template chapters | done |
| REQ-0003 | resource template instruments | done |
| REQ-0011 | resource template speakers | done |
| REQ-0001 | resource template summary (file-backed) | done |
| REQ-0005/0006 | tool `find_mentions` | done |
| REQ-0007 | tool `get_subject_discussion` | done |
| REQ-0010 | tool `get_speaker_coverage` | done |
| REQ-0015 | tool `rank_subjects` | done |
| REQ-0017 | tool `list_rubric_occurrences` | done |
| REQ-0004 | prompt `summarize_live_portion` | done |
| REQ-0013 | prompt `compare_views` | done |

Pinned `mcp` version: `2.3.0` (exact pin in `pyproject.toml`, alongside `pydantic==2.14.2`). Python 3.12.2, `uv` 0.13.1.