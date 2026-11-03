#!/usr/bin/env python
"""Field-level diff between two sets of canonical files (CLAUDE.md section 4.5).

    uv run python scripts/diff_canonical.py [--data-dir DIR] [--left DIR] [--right DIR]

Defaults compare the JSON route (data/canonical) with the txt route
(data/canonical_txt). Segments are compared by position, because the two routes
number segment ids differently. Mentions are compared as per-instrument sets of
segment positions, with recall and precision of the right side against the left.

Differences are reported, never resolved: neither side is preferred.
Exit status is 0 when both sides have the same lives, whatever the differences.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from easygoing_mcp.config import load_settings  # noqa: E402
from easygoing_mcp.models.canonical import CanonicalLive  # noqa: E402

SEGMENT_FIELDS = ("start_s", "end_s", "start_quality", "chapter_id", "text", "mixed_speakers")
SPEAKER_FIELDS = ("speaker_id", "needs_audio_review", "confidence")
CHAPTER_FIELDS = ("start_s", "start_quality", "label_raw", "rubric_id", "speaker_ids", "topics_raw")


@dataclass
class MentionDiff:
    common: int = 0
    only_left: dict[str, list[int]] = field(default_factory=dict)
    only_right: dict[str, list[int]] = field(default_factory=dict)

    @property
    def left_total(self) -> int:
        return self.common + sum(len(v) for v in self.only_left.values())

    @property
    def right_total(self) -> int:
        return self.common + sum(len(v) for v in self.only_right.values())

    @property
    def recall(self) -> float:
        return self.common / self.left_total if self.left_total else 1.0

    @property
    def precision(self) -> float:
        return self.common / self.right_total if self.right_total else 1.0


@dataclass
class LiveDiff:
    live_id: str
    live_fields: dict[str, tuple[object, object]] = field(default_factory=dict)
    coverage_fields: dict[str, tuple[object, object]] = field(default_factory=dict)
    chapter_count: tuple[int, int] = (0, 0)
    chapter_fields: Counter[str] = field(default_factory=Counter)
    segment_count: tuple[int, int] = (0, 0)
    segment_fields: Counter[str] = field(default_factory=Counter)
    segment_examples: dict[str, list[str]] = field(default_factory=dict)
    chapter_examples: dict[str, list[str]] = field(default_factory=dict)
    mentions: MentionDiff = field(default_factory=MentionDiff)
    anomalies: dict[str, tuple[int, int]] = field(default_factory=dict)


def _mention_positions(live: CanonicalLive) -> dict[str, set[int]]:
    position = {s.segment_id: i for i, s in enumerate(live.segments)}
    out: dict[str, set[int]] = defaultdict(set)
    for mention in live.mentions:
        out[mention.instrument_id].add(position[mention.segment_id])
    return out


def _example(store: dict[str, list[str]], key: str, text: str, limit: int = 3) -> None:
    bucket = store.setdefault(key, [])
    if len(bucket) < limit:
        bucket.append(text)


def diff_lives(left: CanonicalLive, right: CanonicalLive) -> LiveDiff:
    result = LiveDiff(live_id=left.live.live_id)

    for name in ("date", "title", "programme_id", "video_url", "duration_seconds", "source_format", "source_file"):
        a, b = getattr(left.live, name), getattr(right.live, name)
        if a != b:
            result.live_fields[name] = (a, b)
    for name in ("content_end_s", "truncated", "mention_index_scope", "timestamps_kind"):
        a, b = getattr(left.coverage, name), getattr(right.coverage, name)
        if a != b:
            result.coverage_fields[name] = (a, b)

    result.chapter_count = (len(left.chapters), len(right.chapters))
    for a, b in zip(left.chapters, right.chapters):
        for name in CHAPTER_FIELDS:
            if getattr(a, name) != getattr(b, name):
                result.chapter_fields[name] += 1
                _example(result.chapter_examples, name, f"{a.chapter_id}: {getattr(a, name)!r} / {getattr(b, name)!r}")

    result.segment_count = (len(left.segments), len(right.segments))
    for i, (a, b) in enumerate(zip(left.segments, right.segments)):
        for name in SEGMENT_FIELDS:
            if getattr(a, name) != getattr(b, name):
                result.segment_fields[name] += 1
                _example(result.segment_examples, name, f"#{i + 1}: {str(getattr(a, name))[:40]!r} / {str(getattr(b, name))[:40]!r}")
        for name in SPEAKER_FIELDS:
            if getattr(a.speaker, name) != getattr(b.speaker, name):
                result.segment_fields[f"speaker.{name}"] += 1
                _example(result.segment_examples, f"speaker.{name}", f"#{i + 1}: {getattr(a.speaker, name)!r} / {getattr(b.speaker, name)!r}")
        if set(a.source) != set(b.source):
            result.segment_fields["source keys"] += 1

    lm, rm = _mention_positions(left), _mention_positions(right)
    for instrument in sorted(lm.keys() | rm.keys()):
        a, b = lm.get(instrument, set()), rm.get(instrument, set())
        result.mentions.common += len(a & b)
        if a - b:
            result.mentions.only_left[instrument] = sorted(p + 1 for p in a - b)
        if b - a:
            result.mentions.only_right[instrument] = sorted(p + 1 for p in b - a)

    la, ra = Counter(x.kind for x in left.anomalies), Counter(x.kind for x in right.anomalies)
    for kind in sorted(la.keys() | ra.keys()):
        result.anomalies[kind] = (la[kind], ra[kind])
    return result


def load_dir(directory: Path) -> dict[str, CanonicalLive]:
    lives = {}
    for path in sorted(directory.glob("*.canonical.json")):
        live = CanonicalLive.model_validate_json(path.read_text(encoding="utf-8"))
        lives[live.live.live_id] = live
    return lives


def render(diff: LiveDiff, left_name: str, right_name: str) -> list[str]:
    out = [f"\n{diff.live_id}   (left = {left_name}, right = {right_name})"]
    for title, fields in (("live", diff.live_fields), ("coverage", diff.coverage_fields)):
        if fields:
            for name, (a, b) in fields.items():
                out.append(f"  {title}.{name}: {a!r}  |  {b!r}")
        else:
            out.append(f"  {title}: identical")

    out.append(f"  chapters: {diff.chapter_count[0]} | {diff.chapter_count[1]}"
               + ("" if diff.chapter_fields else ", identical fields"))
    for name, count in sorted(diff.chapter_fields.items()):
        out.append(f"    {name}: {count} differ, e.g. {diff.chapter_examples.get(name, [])[:2]}")

    out.append(f"  segments: {diff.segment_count[0]} | {diff.segment_count[1]}"
               + ("" if diff.segment_fields else ", identical fields"))
    for name, count in sorted(diff.segment_fields.items()):
        examples = diff.segment_examples.get(name, [])[:2]
        out.append(f"    {name}: {count} differ" + (f", e.g. {examples}" if examples else ""))

    m = diff.mentions
    out.append(
        f"  mentions: left {m.left_total}, right {m.right_total}, common {m.common} "
        f"(right recall {m.recall:.0%}, precision {m.precision:.0%})"
    )
    for instrument, positions in m.only_left.items():
        out.append(f"    only left  {instrument}: segments #{positions}")
    for instrument, positions in m.only_right.items():
        out.append(f"    only right {instrument}: segments #{positions}")

    out.append("  anomalies: " + ", ".join(
        f"{kind} {a}|{b}" + ("" if a == b else " *") for kind, (a, b) in diff.anomalies.items()
    ))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--left", type=Path, default=None, help="default: <data>/canonical")
    parser.add_argument("--right", type=Path, default=None, help="default: <data>/canonical_txt")
    args = parser.parse_args(argv)

    env = {} if args.data_dir is None else {"EASYGOING_DATA_DIR": str(args.data_dir)}
    settings = load_settings({**dict(os.environ), **env})
    left_dir = args.left or settings.canonical_json_dir
    right_dir = args.right or settings.canonical_txt_dir

    left, right = load_dir(left_dir), load_dir(right_dir)
    print("=" * 78)
    print(f"CANONICAL DIFF  {left_dir}  vs  {right_dir}")
    print("=" * 78)
    for live_id in sorted(left.keys() & right.keys()):
        for line in render(diff_lives(left[live_id], right[live_id]), left_dir.name, right_dir.name):
            print(line)

    only = sorted(left.keys() ^ right.keys())
    if only:
        print(f"\nlives on one side only: {only}")
        return 1
    print("\nNeither side is preferred; review the differences before choosing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
