#!/usr/bin/env python
"""Integrity report on canonical files and registries (section 4.5).

    uv run python scripts/validate_data.py [--data-dir DIR] [--canonical-source json|txt] [--compare-dir DIR]

`--canonical-source txt` (or EASYGOING_CANONICAL_SOURCE=txt) validates the txt
route's output in data/canonical_txt against the same expected counts.

Checks referential integrity, ordering, and the counts the specification and
the request catalog both assert. Any count mismatch fails the run: the counts
are how we know the adapters reproduced the sources rather than merely parsed
them.
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

from easygoing_mcp.config import load_settings  # noqa: E402
from easygoing_mcp.models.canonical import CanonicalLive  # noqa: E402

# Section 4.5. These are the specification's numbers, restated by the catalog's
# lives_processed.schema_issues. They are asserted, never derived from output.
EXPECTED: dict[str, dict[str, int]] = {
    "MB20261028": {"segments": 137, "zero_duration": 25, "audio_review": 72, "chapter_conflicts": 0},
    "MB20261029": {"segments": 241, "zero_duration": 31, "audio_review": 52, "chapter_conflicts": 19},
}


@dataclass
class Findings:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def check(self, label: str, passed: bool, detail: str = "") -> None:
        self.checks.append((label, passed, detail))
        if not passed:
            self.failures.append(f"{label}: {detail}")

    def note(self, message: str) -> None:
        self.notes.append(message)


def _load_registry_ids(path: Path, key: str) -> set[str]:
    if not path.exists():
        return set()
    return {entry[key] for entry in json.loads(path.read_text(encoding="utf-8"))}


def validate_live(canonical: CanonicalLive, registries: dict[str, set[str]], findings: Findings) -> None:
    live_id = canonical.live.live_id
    segments = canonical.segments
    chapter_ids = {c.chapter_id for c in canonical.chapters}
    segment_ids = {s.segment_id for s in segments}

    # --- referential integrity -------------------------------------------
    dangling_chapters = sorted(
        s.segment_id for s in segments if s.chapter_id is not None and s.chapter_id not in chapter_ids
    )
    findings.check(
        f"{live_id} segment -> chapter",
        not dangling_chapters,
        f"{len(dangling_chapters)} segments reference an unknown chapter: {dangling_chapters[:5]}",
    )

    dangling_mentions = sorted(
        m.segment_id for m in canonical.mentions if m.segment_id not in segment_ids
    )
    findings.check(
        f"{live_id} mention -> segment",
        not dangling_mentions,
        f"{len(dangling_mentions)} mentions reference an unknown segment: {dangling_mentions[:5]}",
    )

    anomaly_refs = sorted(
        sid for a in canonical.anomalies for sid in a.segment_ids if sid not in segment_ids
    )
    findings.check(
        f"{live_id} anomaly -> segment",
        not anomaly_refs,
        f"{len(anomaly_refs)} anomalies reference an unknown segment: {anomaly_refs[:5]}",
    )

    # --- registry integrity ----------------------------------------------
    used_speakers = {s.speaker.speaker_id for s in segments}
    used_speakers |= {sid for c in canonical.chapters for sid in c.speaker_ids}
    missing_speakers = sorted(used_speakers - registries["speakers"])
    findings.check(
        f"{live_id} speaker ids -> speakers.json",
        not missing_speakers,
        f"not in the registry: {missing_speakers}",
    )

    used_rubrics = {c.rubric_id for c in canonical.chapters if c.rubric_id is not None}
    missing_rubrics = sorted(used_rubrics - registries["rubrics"])
    findings.check(
        f"{live_id} rubric ids -> rubrics.json",
        not missing_rubrics,
        f"not in the registry: {missing_rubrics}",
    )

    used_instruments = {m.instrument_id for m in canonical.mentions}
    missing_instruments = sorted(used_instruments - registries["instruments"])
    findings.check(
        f"{live_id} instrument ids -> instruments.json",
        not missing_instruments,
        f"not in the registry: {missing_instruments}",
    )

    findings.check(
        f"{live_id} programme id -> programmes.json",
        canonical.live.programme_id in registries["programmes"],
        f"{canonical.live.programme_id!r} not in the registry",
    )

    # --- ordering and durations ------------------------------------------
    starts = [s.start_s for s in segments]
    out_of_order = [
        segments[i].segment_id for i in range(1, len(starts)) if starts[i] < starts[i - 1]
    ]
    findings.check(
        f"{live_id} start_s monotonic",
        not out_of_order,
        f"{len(out_of_order)} segments start before their predecessor: {out_of_order[:5]}",
    )

    negative = [s.segment_id for s in segments if s.end_s < s.start_s]
    findings.check(
        f"{live_id} end_s >= start_s",
        not negative,
        f"{len(negative)} segments end before they start: {negative[:5]}",
    )

    chapter_starts = [c.start_s for c in canonical.chapters]
    findings.check(
        f"{live_id} chapter start_s monotonic",
        chapter_starts == sorted(chapter_starts),
        f"chapters are not ordered by start_s: {chapter_starts}",
    )

    # --- the asserted counts ----------------------------------------------
    expected = EXPECTED.get(live_id)
    if expected is None:
        findings.note(f"{live_id}: no expected counts on record; counts not asserted")
        return

    kinds = Counter(a.kind for a in canonical.anomalies)
    actual = {
        "segments": len(segments),
        "zero_duration": kinds["zero_duration"],
        "audio_review": sum(1 for s in segments if s.speaker.needs_audio_review),
        "chapter_conflicts": kinds["chapter_assignment_conflict"],
    }
    for name, want in expected.items():
        got = actual[name]
        findings.check(f"{live_id} count {name}", got == want, f"expected {want}, got {got}")


def cross_check_catalog(canonicals: list[CanonicalLive], catalog_path: Path, findings: Findings) -> None:
    """Compare canonical output with what the catalog records about each live.

    Differences are reported, never silently accommodated (section 4.5).
    """
    if not catalog_path.exists():
        findings.note(f"catalog not found at {catalog_path}; catalog cross-check skipped")
        return

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    by_id = {entry["live_id"]: entry for entry in catalog.get("lives_processed", [])}
    findings.note(
        f"catalog v{catalog.get('catalog_version')} "
        f"(ingest_schema_version {catalog.get('ingest_schema_version')}) "
        f"records {len(by_id)} processed lives"
    )

    for canonical in canonicals:
        entry = by_id.get(canonical.live.live_id)
        if entry is None:
            findings.note(f"{canonical.live.live_id}: not recorded in the catalog")
            continue

        # The catalog records the JSON source each live was first built from. A live
        # built by the txt route comes from a different file by construction, so
        # those two fields are noted, not failed; everything else is still checked.
        from_txt = canonical.live.source_format == "transcript_txt_v1"
        if from_txt:
            findings.note(
                f"{canonical.live.live_id}: built by the txt route from {canonical.live.source_file}; "
                f"the catalog's source_format/source_file ({entry.get('source_format')}, "
                f"{entry.get('source_file')}) describe the JSON route and are not compared"
            )
        for field_name, ours in (
            ("date", canonical.live.date),
            *(() if from_txt else (
                ("source_format", canonical.live.source_format),
                ("source_file", canonical.live.source_file),
            )),
            ("title", canonical.live.title),
            ("duration_seconds", canonical.live.duration_seconds),
        ):
            if field_name not in entry:
                continue
            theirs = entry[field_name]
            findings.check(
                f"{canonical.live.live_id} catalog {field_name}",
                theirs == ours,
                f"catalog says {theirs!r}, ingest produced {ours!r}",
            )

        expected_name = entry.get("canonical_file")
        if expected_name:
            findings.check(
                f"{canonical.live.live_id} canonical file name",
                expected_name == f"{canonical.live.live_id}.canonical.json",
                f"catalog expects {expected_name!r}",
            )


def diff_external(canonicals: list[CanonicalLive], compare_dir: Path | None, findings: Findings) -> None:
    """Diff against canonical files produced elsewhere, when provided (section 4.5).

    Neither output is preferred; differences are only reported.
    """
    if compare_dir is None:
        findings.note(
            "no --compare-dir given: the catalog references externally produced canonical "
            "files, but none were supplied, so there is nothing to diff"
        )
        return

    for canonical in canonicals:
        other_path = compare_dir / f"{canonical.live.live_id}.canonical.json"
        if not other_path.exists():
            findings.note(f"{canonical.live.live_id}: no counterpart in {compare_dir}")
            continue
        other: Any = json.loads(other_path.read_text(encoding="utf-8"))
        ours: Any = canonical.model_dump(mode="json")
        if other == ours:
            findings.note(f"{canonical.live.live_id}: identical to {other_path}")
            continue
        differing = sorted(
            key for key in set(ours) | set(other) if ours.get(key) != other.get(key)
        )
        findings.note(
            f"{canonical.live.live_id}: differs from {other_path} in {differing} "
            "(neither version preferred; review before choosing)"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument(
        "--compare-dir", type=Path, default=None,
        help="directory of canonical files produced elsewhere, to diff against",
    )
    parser.add_argument(
        "--canonical-source", choices=("json", "txt"), default=None,
        help="which route's canonical files to validate (default: EASYGOING_CANONICAL_SOURCE, else json)",
    )
    args = parser.parse_args(argv)

    import os
    env = {} if args.data_dir is None else {"EASYGOING_DATA_DIR": str(args.data_dir)}
    if args.canonical_source is not None:
        env["EASYGOING_CANONICAL_SOURCE"] = args.canonical_source
    settings = load_settings({**dict(os.environ), **env})

    paths = sorted(settings.canonical_dir.glob("*.canonical.json"))
    if not paths:
        print(
            f"no canonical files in {settings.canonical_dir}. Run scripts/ingest.py first.",
            file=sys.stderr,
        )
        return 1

    findings = Findings()
    canonicals: list[CanonicalLive] = []
    for path in paths:
        try:
            canonicals.append(
                CanonicalLive.model_validate_json(path.read_text(encoding="utf-8"))
            )
        except Exception as exc:  # noqa: BLE001 - reported, then fails the run
            findings.check(f"{path.name} parses as canonical", False, str(exc).replace("\n", " ")[:300])

    registries = {
        "speakers": _load_registry_ids(settings.registries_dir / "speakers.json", "speaker_id"),
        "rubrics": _load_registry_ids(settings.registries_dir / "rubrics.json", "rubric_id"),
        "instruments": _load_registry_ids(settings.registries_dir / "instruments.json", "instrument_id"),
        "programmes": _load_registry_ids(settings.registries_dir / "programmes.json", "programme_id"),
    }
    for name, ids in registries.items():
        findings.check(f"registry {name}.json present", bool(ids), "missing or empty")

    for canonical in canonicals:
        validate_live(canonical, registries, findings)

    cross_check_catalog(canonicals, settings.catalog_file, findings)
    diff_external(canonicals, args.compare_dir, findings)

    width = max(len(label) for label, _, _ in findings.checks) + 2
    print("=" * 78)
    print(f"VALIDATION REPORT — {settings.canonical_dir}")
    print("=" * 78)
    for label, passed, detail in findings.checks:
        mark = "PASS" if passed else "FAIL"
        suffix = "" if passed else f"  <- {detail}"
        print(f"  [{mark}] {label.ljust(width)}{suffix}")

    if findings.notes:
        print("\nNOTES")
        for note in findings.notes:
            print(f"  - {note}")

    passed_count = sum(1 for _, ok, _ in findings.checks if ok)
    print(f"\n{passed_count}/{len(findings.checks)} checks passed")
    if findings.failures:
        print(f"\nFAILED ({len(findings.failures)}):")
        for failure in findings.failures:
            print(f"  ! {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
