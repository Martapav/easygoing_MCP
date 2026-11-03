#!/usr/bin/env python
"""Build canonical files from source transcripts and seed the registries.

    uv run python scripts/ingest.py [--data-dir DIR] [--dry-run]

Source files are read-only inputs; canonical files are build output and are
never hand-edited. Registries are seeded here and curated by hand afterwards,
so this script only ever adds ids and aliases — it never overwrites a field a
human may have filled in (section 4.4).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from easygoing_mcp.config import Settings, load_settings  # noqa: E402
from easygoing_mcp.ingest import mb_mymeeting_en_v0 as en_v0  # noqa: E402
from easygoing_mcp.ingest import mb_mymeeting_it_v1 as it_v1  # noqa: E402
from easygoing_mcp.ingest.detect import detect_format  # noqa: E402
from easygoing_mcp.ingest.slugs import normalize, slugify  # noqa: E402
from easygoing_mcp.ingest.timecodes import format_timecode, parse_timecode  # noqa: E402
from easygoing_mcp.models.canonical import CanonicalLive  # noqa: E402
from easygoing_mcp.models.source_en_v0 import EnV0Document  # noqa: E402
from easygoing_mcp.models.source_it_v1 import ItV1Document  # noqa: E402


@dataclass
class Seed:
    """Display names gathered from a source file, keyed by the id they slug to.

    Registries need names and tickers, which canonical files deliberately do not
    carry (they store ids only). The seed is collected here, from the raw
    document, rather than widening the canonical schema.
    """

    speakers: dict[str, str] = field(default_factory=dict)
    rubrics: dict[str, str] = field(default_factory=dict)
    programmes: dict[str, str] = field(default_factory=dict)
    # instrument_id -> (display name, raw ticker string or None)
    instruments: dict[str, tuple[str, str | None]] = field(default_factory=dict)


@dataclass
class Report:
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.notes.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def split_tickers(raw: str | None) -> list[str]:
    """`GOOGL / GOOG` and `GOOGL/GOOG` both become `[GOOGL, GOOG]`.

    Splits on `/` only. An exchange suffix such as `000660 (KRX)` is preserved
    verbatim, because dropping it would invent a bare ticker the source never
    stated (section 4.4, "record each ticker as given").
    """
    if not raw:
        return []
    return [part.strip() for part in raw.split("/") if part.strip()]


# --------------------------------------------------------------------------
# Cross-checks: the source index states first mentions and speaker counts.
# They are recomputed from the canonical data and compared, never stored
# (sections 4.2 and 4.3).
# --------------------------------------------------------------------------

def cross_check_en_v0(document: EnV0Document, canonical: CanonicalLive, report: Report) -> None:
    by_id = {segment.segment_id: segment for segment in canonical.segments}
    live_id = canonical.live.live_id

    for security in document.securities_index:
        segments = [by_id[sid] for sid in security.segments]

        # Compared in seconds: the source writes mm:ss, canonical start_s is an int.
        recomputed_first = min(s.start_s for s in segments)
        stated_first = parse_timecode(security.first_mention)
        if recomputed_first != stated_first:
            report.warn(
                f"{live_id}: {security.name!r} first_mention stated "
                f"{security.first_mention!r} ({stated_first} s), recomputed "
                f"{format_timecode(recomputed_first)} ({recomputed_first} s)"
            )

        recomputed_speakers = {s.speaker.speaker_id for s in segments}
        stated_speakers = {slugify(name) for name in security.speakers}
        if recomputed_speakers != stated_speakers:
            report.warn(
                f"{live_id}: {security.name!r} speakers stated {sorted(stated_speakers)}, "
                f"recomputed {sorted(recomputed_speakers)}"
            )


def cross_check_it_v1(document: ItV1Document, canonical: CanonicalLive, report: Report) -> None:
    by_id = {segment.segment_id: segment for segment in canonical.segments}
    live_id = canonical.live.live_id

    for instrument in document.titoli_e_strumenti:
        segment_ids = [f"{live_id}-{raw:03d}" for raw in instrument.segmenti_ids]
        segments = [by_id[sid] for sid in segment_ids]

        if instrument.occorrenze_segmenti != len(segment_ids):
            report.warn(
                f"{live_id}: {instrument.nome!r} occorrenze_segmenti says "
                f"{instrument.occorrenze_segmenti}, segmenti_ids has {len(segment_ids)}"
            )

        recomputed = Counter(s.speaker.speaker_id for s in segments)
        stated = Counter({slugify(name): count for name, count in instrument.speaker.items()})
        if recomputed != stated:
            report.warn(
                f"{live_id}: {instrument.nome!r} per-speaker counts stated {dict(sorted(stated.items()))}, "
                f"recomputed {dict(sorted(recomputed.items()))}"
            )


# --------------------------------------------------------------------------
# Seeds
# --------------------------------------------------------------------------

def seed_en_v0(document: EnV0Document, canonical: CanonicalLive) -> Seed:
    seed = Seed()
    for segment in document.segments:
        seed.speakers[slugify(segment.speaker)] = segment.speaker

    for chapter in canonical.chapters:
        if chapter.rubric_id is not None:
            # label_raw is `Left — Right`; the rubric is the left side.
            name = chapter.label_raw.split("—", 1)[0].strip()
            seed.rubrics[chapter.rubric_id] = name

    for security in document.securities_index:
        seed.instruments[slugify(security.name)] = (security.name, security.ticker)

    seed.programmes[canonical.live.programme_id] = "Market Briefing"
    return seed


def seed_it_v1(document: ItV1Document, canonical: CanonicalLive) -> Seed:
    seed = Seed()
    for segment in document.segmenti:
        seed.speakers[slugify(segment.speaker)] = segment.speaker
    for name in document.speaker_ufficiali:
        seed.speakers.setdefault(slugify(name), name)

    for chapter in canonical.chapters:
        if chapter.rubric_id is not None:
            seed.rubrics[chapter.rubric_id] = chapter.label_raw

    for instrument in document.titoli_e_strumenti:
        seed.instruments[slugify(instrument.nome)] = (instrument.nome, instrument.ticker)

    seed.programmes[canonical.live.programme_id] = document.programma
    return seed


# --------------------------------------------------------------------------
# Registry merge: additive only
# --------------------------------------------------------------------------

def _load_registry(path: Path, key: str) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {entry[key]: entry for entry in payload}


def _add_alias(entry: dict[str, Any], alias: str) -> bool:
    """Add an alias if it is new and not just the name restated."""
    if normalize(alias) == normalize(entry.get("name", "")):
        return False
    aliases = entry.setdefault("aliases", [])
    if any(normalize(alias) == normalize(existing) for existing in aliases):
        return False
    aliases.append(alias)
    return True


def merge_registries(seeds: list[Seed], settings: Settings, report: Report, dry_run: bool) -> None:
    directory = settings.registries_dir
    directory.mkdir(parents=True, exist_ok=True)

    speakers = _load_registry(directory / "speakers.json", "speaker_id")
    rubrics = _load_registry(directory / "rubrics.json", "rubric_id")
    instruments = _load_registry(directory / "instruments.json", "instrument_id")
    programmes = _load_registry(directory / "programmes.json", "programme_id")

    added = Counter()

    for seed in seeds:
        for speaker_id, name in sorted(seed.speakers.items()):
            if speaker_id not in speakers:
                # role, affiliation and disclosures stay null/empty: they come from
                # the user, never from the transcripts (section 4.4).
                speakers[speaker_id] = {
                    "speaker_id": speaker_id, "name": name, "aliases": [],
                    "role": None, "affiliation": None, "links": [], "disclosures": [],
                }
                added["speakers"] += 1
            elif _add_alias(speakers[speaker_id], name):
                added["speaker aliases"] += 1

        for rubric_id, name in sorted(seed.rubrics.items()):
            if rubric_id not in rubrics:
                rubrics[rubric_id] = {"rubric_id": rubric_id, "name": name, "aliases": []}
                added["rubrics"] += 1
            elif _add_alias(rubrics[rubric_id], name):
                added["rubric aliases"] += 1

        for programme_id, name in sorted(seed.programmes.items()):
            if programme_id not in programmes:
                programmes[programme_id] = {"programme_id": programme_id, "name": name}
                added["programmes"] += 1

        for instrument_id, (name, raw_ticker) in sorted(seed.instruments.items()):
            tickers = split_tickers(raw_ticker)
            if instrument_id not in instruments:
                instruments[instrument_id] = {
                    "instrument_id": instrument_id, "name": name, "aliases": [],
                    "tickers": tickers, "asset_class": None,  # never guessed (section 4.4)
                }
                added["instruments"] += 1
                continue

            entry = instruments[instrument_id]
            if _add_alias(entry, name):
                added["instrument aliases"] += 1

            existing = entry.get("tickers") or []
            if tickers and existing and tickers != existing:
                # Keep both; do not resolve automatically (section 4.4).
                merged = existing + [t for t in tickers if t not in existing]
                entry["tickers"] = merged
                report.warn(
                    f"instrument {instrument_id!r} ({name}): ticker conflict across lives — "
                    f"had {existing}, this live says {tickers}; kept both as {merged}"
                )
            elif tickers and not existing:
                entry["tickers"] = tickers
                report.warn(
                    f"instrument {instrument_id!r} ({name}): ticker missing in one live, "
                    f"present as {tickers} in another; kept the stated value"
                )
            elif existing and not tickers:
                report.warn(
                    f"instrument {instrument_id!r} ({name}): ticker missing in this live, "
                    f"kept {existing} from another"
                )

    if not dry_run:
        _write(directory / "speakers.json", [speakers[k] for k in sorted(speakers)])
        _write(directory / "rubrics.json", [rubrics[k] for k in sorted(rubrics)])
        _write(directory / "instruments.json", [instruments[k] for k in sorted(instruments)])
        _write(directory / "programmes.json", [programmes[k] for k in sorted(programmes)])

    report.note(
        "registries: "
        + (", ".join(f"{count} {label}" for label, count in sorted(added.items())) or "nothing new")
        + f" (speakers {len(speakers)}, rubrics {len(rubrics)}, "
        f"instruments {len(instruments)}, programmes {len(programmes)})"
    )
    # Section 4.4: there is deliberately no topics registry.
    report.note("registries: no topics registry by design; topic subjects resolve by text match only")


def _write(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------

def _report_cross_check(report: Report, live_id: str, warnings_before: int, index_name: str) -> None:
    """State the cross-check outcome, so a pass is visible and not just an absence."""
    found = len(report.warnings) - warnings_before
    if found:
        report.note(f"{live_id}: {found} {index_name} cross-check disagreements (see FLAGGED)")
    else:
        report.note(
            f"{live_id}: {index_name} first mentions and per-speaker counts agree with the "
            "values recomputed from segments"
        )


def ingest_file(path: Path, settings: Settings, report: Report) -> tuple[CanonicalLive, Seed]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    source_format = detect_format(raw)
    report.note(f"{path.name}: detected {source_format}")

    if source_format == "mb_mymeeting_en_v0":
        document = EnV0Document.model_validate(raw)
        canonical, notes = en_v0.adapt(document, path.name)
        seed = seed_en_v0(document, canonical)
        before = len(report.warnings)
        cross_check_en_v0(document, canonical, report)
        _report_cross_check(report, canonical.live.live_id, before, "securities_index")
    else:
        document = ItV1Document.model_validate(raw)
        canonical, notes = it_v1.adapt(document, path.name, settings)
        seed = seed_it_v1(document, canonical)
        before = len(report.warnings)
        cross_check_it_v1(document, canonical, report)
        _report_cross_check(report, canonical.live.live_id, before, "titoli_e_strumenti")

    without_ticker = sorted(name for name, ticker in seed.instruments.values() if not ticker)
    if without_ticker:
        report.note(
            f"{canonical.live.live_id}: {len(without_ticker)} of {len(seed.instruments)} indexed "
            f"instruments carry no ticker: {without_ticker}"
        )

    for note in notes:
        report.note(note)
    return canonical, seed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="report without writing files")
    args = parser.parse_args(argv)

    env = {} if args.data_dir is None else {"EASYGOING_DATA_DIR": str(args.data_dir)}
    settings = load_settings({**dict(__import__("os").environ), **env})

    report = Report()
    sources = sorted(settings.source_dir.glob("*.json"))
    if not sources:
        print(f"no source files in {settings.source_dir}", file=sys.stderr)
        return 1

    canonicals: list[CanonicalLive] = []
    seeds: list[Seed] = []
    for path in sources:
        canonical, seed = ingest_file(path, settings, report)
        canonicals.append(canonical)
        seeds.append(seed)

    if not args.dry_run:
        settings.canonical_dir.mkdir(parents=True, exist_ok=True)
        for canonical in canonicals:
            target = settings.canonical_dir / f"{canonical.live.live_id}.canonical.json"
            _write(target, canonical.model_dump(mode="json"))
            report.note(f"wrote {target.relative_to(settings.data_dir.parent)}")

    merge_registries(seeds, settings, report, args.dry_run)

    print("=" * 78)
    print("INGEST REPORT" + ("  (dry run — nothing written)" if args.dry_run else ""))
    print("=" * 78)
    for canonical in canonicals:
        kinds = Counter(a.kind for a in canonical.anomalies)
        print(
            f"\n{canonical.live.live_id}  {canonical.live.date}  {canonical.live.source_format}"
            f"\n  segments {len(canonical.segments)}, chapters {len(canonical.chapters)}, "
            f"mentions {len(canonical.mentions)}, instruments "
            f"{len({m.instrument_id for m in canonical.mentions})}"
            f"\n  coverage: content_end_s={canonical.coverage.content_end_s}, "
            f"truncated={canonical.coverage.truncated}, "
            f"scope={canonical.coverage.mention_index_scope}, "
            f"timestamps={canonical.coverage.timestamps_kind}"
            f"\n  audio review: {sum(1 for s in canonical.segments if s.speaker.needs_audio_review)}"
            f"\n  anomalies: {dict(sorted(kinds.items())) or 'none'}"
        )

    print("\nNOTES")
    for note in report.notes:
        print(f"  - {note}")

    print(f"\nFLAGGED ({len(report.warnings)}) — not resolved automatically")
    for warning in report.warnings:
        print(f"  ! {warning}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
