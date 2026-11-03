# EasyGoing transcript .txt format (`transcript_txt_v1`)

This is the standard input for a live broadcast. A `.txt` written to this spec is converted by
`scripts/ingest.py --route txt` into a canonical JSON (`data/canonical_txt/<live_id>.canonical.json`)
that the MCP server reads like any other live. The parser is **strict**: a line it cannot
classify stops the ingest with its line number. Nothing is guessed.

It is written for whoever prepares the transcript. Section 7 is the checklist.

---

## 1. File name

```
<Programme_Name>_<YYYY-MM-DD>_<free suffix>.txt
e.g. Market_Briefing_2026-10-30_TRASCRIZIONE_CORRETTA.txt
```

- `Programme_Name` gives the programme id (`Market_Briefing` becomes `market-briefing`) and the
  live id prefix, which is the initials of its words (`MB`). The live id is prefix + date,
  e.g. `MB20261030`. Segment ids are `<live_id>-001`, `-002`, … in file order.
- The date must agree with the title line (section 3).
- Encoding: UTF-8.

## 2. Metadata block (recommended, at the very top)

```
---
title: E poi venne la Fed...
video_url: https://youtube.com/live/XXXXXXXXXXX
content_end: 1:39:01
truncated: false
mention_index_scope: multi_asset
timestamps: asr_chunk_start
guests: Davide Biocchi, Paolo Serafini
---
```

| key | meaning | values |
|---|---|---|
| `title` | episode title; omit or leave empty if none (the programme name is **not** a title) | text |
| `video_url` | public video; enables deep links | URL, or empty |
| `content_end` | YouTube time where the broadcast content ends | `mm:ss` / `h:mm:ss` |
| `truncated` | the transcript stops before the broadcast ends | `true` / `false` |
| `mention_index_scope` | what the index (section 5) covers | `equities_only` / `multi_asset` |
| `timestamps` | what segment times are | `asr_chunk_start` / `estimated_interpolated` |
| `guests` | official guest list, comma-separated | names |

Unknown keys and malformed values are errors. Each key can also be stated outside the block,
for the few that have a line form (`Video:` and `OSPITI…:` lines, a `~` on the timeline clock).

**Temporary fallback.** The first two .txt files (28 and 29 Oct 2026) have no metadata block.
For now, a field missing from the .txt is taken from the companion JSON source of the same
programme and date in `data/source/`, and every borrowed field is listed in the canonical file
(`live.source_meta.metadata_fallback`) and in the ingest report. New files should not rely on
this. If a required field has no source, ingest fails.

Required fields: `content_end`, `truncated`, `mention_index_scope`, `timestamps`.
`title`, `video_url` and `guests` are optional. When the live is not truncated, `content_end`
is also used as the duration.
Without a `content_end`, the last segment's start is used only if `truncated: true`, because then
the broadcast end is unknown by definition.

## 3. Title line

The first line after the metadata block:

```
MARKET BRIEFING — 28 OTTOBRE 2026
```

The programme name, an em dash `—` (or ` - `), then the day, the Italian month name and the
year. The date is cross-checked against the file name.

## 4. Preamble (between the title line and the transcript)

The preamble ends at the first segment line or section header (section 6). Before that, lines
are recognised as follows. A heading is a line whose first word is written in capitals and,
ignoring accents, equals or starts with the keyword.

| line | recognised as |
|---|---|
| `Video: <url>` | video URL |
| `OSPITI …: Name, Name, …` | guest list |
| heading starting with `CAPITOLI` | start of the chapter list |
| heading starting with `TITOLI` or `INDICE` | start of the instrument index |
| `---…`, `===…` only | separator |
| anything else | free prose: kept verbatim in `live.source_meta.header_text`, never interpreted |

### 4.1 Chapter lines (after a `CAPITOLI` heading, until a blank line)

```
  00:00  Davide Biocchi — Fed, MSFT, GOOGL, Stellantis
     11:47 | Grafico del giorno — F5 (FFIV)
25:02 [da verificare] | Chiediamolo all’AI — Cosa ha detto la Fed; FedWatch
```

`<timecode> [optional [annotation]] [optional |] <Label>[ — <topics>]`.

- If `Label` is a guest (in the guest list, or a segment speaker), the chapter is a guest block.
  Otherwise it is a rubric, and its id is the slug of the label.
- A bracketed annotation marks the time as unverified (`chapter_official_unverified`).
- Chapter times must not decrease.

## 5. Instrument index (`TITOLI…` / `INDICE…`, before or after the transcript)

One line per instrument or subject. Two forms are accepted:

```
  Stellantis (STLAM) — prima occorrenza 00:00
Stellantis (STLAM) | 10 segmenti | Davide Biocchi, Paolo Serafini
```

- The name comes first. Tickers go in the **last** pair of parentheses, with several separated by
  `/` (`GOOGL / GOOG`). Inner parentheses are kept: `SK hynix (000660 (KRX))`. No parentheses
  means no ticker (`Oro`, `Nasdaq`).
- `A / B` names with **spaced** slashes (`Alphabet / Google`) are searched under each part. An
  unspaced slash is part of one name (`Euro/Dollaro` is a currency pair; `euro` alone is not it).
- Spoken short forms (`Samsung` for `Samsung Electronics`, `Brent` for `Petrolio Brent`) are found
  only if they are curated as aliases in `data/registries/instruments.json`. Ingest never invents
  them. Writing the index name the way speakers say it also helps.
- After the name: either `— prima occorrenza <timecode>`, or `| N segmenti | speakers`. These
  statements are **cross-checked**, not trusted. The mentions are found by searching the segment
  text for the name, its parts and its tickers. Tickers match case-sensitively as whole words.
- The index does not list segments, so a mention found this way is recorded with origin
  `index_text_match`.

## 6. Transcript body

It starts at the first segment line or section header and ends at the last segment line.
Between them, only these lines are allowed:

### 6.1 Segment line (one per line, text on the same line)

```
[00:00] Davide Biocchi: Buongiorno a tutti…
[1:07:22] Giancarlo Prisco [speaker da verificare]: …
[REC 00:04:42 | YT~ 00:00:00] Davide Biocchi [?]: Allora, buongiorno…
```

- **Clock group** in square brackets: either one bare timecode, or several labelled clocks
  separated by `|`. The **timeline** is the bare timecode, or the clock labelled `YT`. A `~`
  after the label (`YT~`) means the time is estimated, which sets
  `timestamps: estimated_interpolated`. A `REC` clock (recording time) is kept in the segment's
  `source`.
- **Speaker**: the name exactly as in the guest list, followed by `:`.
- **Review flag**: any bracketed text between the name and the `:` (`[?]`,
  `[speaker da verificare]`) means the attribution is not verified on the audio
  (`needs_audio_review = true`).
- Times must not decrease. Equal consecutive times are allowed. They produce zero-duration
  segments, which are reported as such.
- Text is kept **verbatim**. Never correct numbers.

### 6.2 Section header (optional)

```
========== GRAFICO DEL GIORNO ==========
```

It names the chapter that the following segments belong to, matched against chapter labels
ignoring case and accents. When headers are present, they decide chapter assignment, and any
disagreement with time-based assignment is recorded as a `chapter_assignment_conflict`. When
there are none, segments are assigned to chapters by time.

### 6.3 Blank and separator lines

Blank lines and lines made only of `=` or `-` are allowed. **Any other line inside the body is an
error.**

## 7. After the transcript

| block | recognised as |
|---|---|
| `TITOLI…` / `INDICE…` heading | instrument index (section 5) |
| heading starting with `NOTE` | quality notes: each `- ` bullet becomes a `source_limitation` caveat shown to users |

Nothing else may follow the transcript.

## 8. Checklist for a new transcript

1. File name `Programme_Name_YYYY-MM-DD_….txt`, UTF-8.
2. Metadata block with all four required keys (`content_end`, `truncated`,
   `mention_index_scope`, `timestamps`), plus `video_url`, `title` and `guests` when known.
3. Title line `PROGRAMME — DD MESE YYYY`.
4. `CAPITOLI…` heading followed by one chapter per line.
5. One segment per line: `[time] Speaker[ [flag]]: text`. Flag any unverified attribution.
6. Optional `===== CHAPTER =====` headers when chapter times are unreliable.
7. `TITOLI…` / `INDICE…` index of instruments, with tickers in parentheses.
8. Optional `NOTE…` bullets for limitations the listener should know about.
9. Run `uv run python scripts/ingest.py --route txt --dry-run` and fix every reported line.
