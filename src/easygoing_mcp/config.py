"""Paths, feature flags and limits.

Everything here is read from the environment with a safe default, so the server
can run from a checkout with no configuration at all. Settings are a frozen
value object rather than module globals: the test suite has to exercise more
than one configuration in a single process (section 6 requires both
advice-risk states), which import-time globals make awkward.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

# Canonical schema version, mirrored from data/catalog.json ingest_schema_version.
# Written into every canonical file so a consumer can detect a schema change.
INGEST_SCHEMA_VERSION = "1.0"

ChapterAssignment = Literal["rubrica", "time"]

# Which ingest route's canonical files the server reads (data/canonical vs data/canonical_txt).
CanonicalSource = Literal["json", "txt"]


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(raw: str | None, default: int) -> int:
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"expected an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    data_dir: Path

    # Section 6: advice-risk primitives are not registered at all unless enabled.
    enable_advice_risk: bool = False

    # Section 5: lists are bounded.
    default_limit: int = 10
    max_limit: int = 50

    # Cap on text embedded into prompt messages (section 7, summarize_live_portion).
    max_prompt_segments: int = 80

    # Characters of transcript text embedded per prompt. A segment count alone is
    # not a cap: one MB20261028 segment is ~10,000 characters. Whole segments are
    # dropped, never cut, and the prompt says when this bites.
    max_prompt_chars: int = 40000

    # Adjacent segments of the same speaker in the same chapter pulled in either
    # direction as context by get_subject_discussion (section 7, REQ-0007).
    context_radius: int = 2

    # Hard cap on segments returned per speaker group by get_subject_discussion,
    # so context expansion can never produce an unbounded list (section 5).
    max_discussion_segments: int = 40

    # Section 4.3: MB20261029 segment times are interpolated and one chapter time is
    # wrong by ~298 s, so chapters are assigned from the per-segment `rubrica` name
    # instead of by time. Flip to "time" to get time-based assignment back.
    chapter_assignment_it_v1: ChapterAssignment = "rubrica"

    # transcript_txt_v1: when a .txt carries `==== SECTION ====` headers, they decide
    # chapter assignment ("rubrica", the same role as it_v1's per-segment rubrica);
    # "time" switches to time-based assignment. Files without headers always use time.
    chapter_assignment_txt_v1: ChapterAssignment = "rubrica"

    # Which ingest route the server serves: "json" (data/canonical, built from the
    # JSON sources) or "txt" (data/canonical_txt, built from the .txt sources).
    canonical_source: CanonicalSource = "json"

    @property
    def source_dir(self) -> Path:
        return self.data_dir / "source"

    @property
    def canonical_dir(self) -> Path:
        """The canonical files the server reads, per `canonical_source`."""
        return self.canonical_txt_dir if self.canonical_source == "txt" else self.canonical_json_dir

    @property
    def canonical_json_dir(self) -> Path:
        return self.data_dir / "canonical"

    @property
    def canonical_txt_dir(self) -> Path:
        return self.data_dir / "canonical_txt"

    @property
    def registries_dir(self) -> Path:
        return self.data_dir / "registries"

    @property
    def summaries_dir(self) -> Path:
        return self.data_dir / "derived" / "summaries"

    @property
    def catalog_file(self) -> Path:
        return self.data_dir / "catalog.json"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from a environment mapping (defaults to os.environ)."""
    env = os.environ if env is None else env

    assignment = env.get("EASYGOING_CHAPTER_ASSIGNMENT_IT_V1", "rubrica").strip().lower()
    if assignment not in {"rubrica", "time"}:
        raise ValueError(
            f"EASYGOING_CHAPTER_ASSIGNMENT_IT_V1 must be 'rubrica' or 'time', got {assignment!r}"
        )

    assignment_txt = env.get("EASYGOING_CHAPTER_ASSIGNMENT_TXT_V1", "rubrica").strip().lower()
    if assignment_txt not in {"rubrica", "time"}:
        raise ValueError(
            f"EASYGOING_CHAPTER_ASSIGNMENT_TXT_V1 must be 'rubrica' or 'time', got {assignment_txt!r}"
        )

    canonical_source = env.get("EASYGOING_CANONICAL_SOURCE", "json").strip().lower()
    if canonical_source not in {"json", "txt"}:
        raise ValueError(
            f"EASYGOING_CANONICAL_SOURCE must be 'json' or 'txt', got {canonical_source!r}"
        )

    return Settings(
        data_dir=Path(env.get("EASYGOING_DATA_DIR", "./data")).expanduser().resolve(),
        enable_advice_risk=_as_bool(env.get("EASYGOING_ENABLE_ADVICE_RISK"), False),
        default_limit=_as_int(env.get("EASYGOING_DEFAULT_LIMIT"), 10),
        max_limit=_as_int(env.get("EASYGOING_MAX_LIMIT"), 50),
        max_prompt_segments=_as_int(env.get("EASYGOING_MAX_PROMPT_SEGMENTS"), 80),
        max_prompt_chars=_as_int(env.get("EASYGOING_MAX_PROMPT_CHARS"), 40000),
        context_radius=_as_int(env.get("EASYGOING_CONTEXT_RADIUS"), 2),
        max_discussion_segments=_as_int(env.get("EASYGOING_MAX_DISCUSSION_SEGMENTS"), 40),
        chapter_assignment_it_v1=assignment,  # type: ignore[arg-type]
        chapter_assignment_txt_v1=assignment_txt,  # type: ignore[arg-type]
        canonical_source=canonical_source,  # type: ignore[arg-type]
    )
