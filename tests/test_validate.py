"""Validation behaviour (section 4.5).

The point of these is that the count assertions actually fail the run. A
validator that only ever passes would tell us nothing about whether the
adapters reproduced the sources.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import validate_data  # noqa: E402  (scripts/ on sys.path via conftest)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_DATA = REPO_ROOT / "data"

pytestmark = pytest.mark.skipif(
    not (REAL_DATA / "canonical" / "MB20261028.canonical.json").exists(),
    reason="canonical files not built; run scripts/ingest.py",
)


@pytest.fixture
def data_dir(tmp_path) -> Path:
    """A writable copy of the real data, so tests can corrupt it safely."""
    target = tmp_path / "data"
    (target / "canonical").mkdir(parents=True)
    shutil.copytree(REAL_DATA / "registries", target / "registries")
    for path in (REAL_DATA / "canonical").glob("*.canonical.json"):
        shutil.copy(path, target / "canonical" / path.name)
    shutil.copy(REAL_DATA / "catalog.json", target / "catalog.json")
    return target


def run(data_dir: Path) -> int:
    return validate_data.main(["--data-dir", str(data_dir)])


class TestRealData:
    def test_passes_on_the_built_canonical_files(self, data_dir):
        assert run(data_dir) == 0


class TestCountsAreEnforced:
    def _rewrite(self, data_dir: Path, live_id: str, mutate) -> None:
        path = data_dir / "canonical" / f"{live_id}.canonical.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_a_missing_segment_fails_the_run(self, data_dir, capsys):
        self._rewrite(data_dir, "MB20261028", lambda p: p["segments"].pop())
        assert run(data_dir) == 1
        assert "count segments" in capsys.readouterr().out

    def test_a_removed_anomaly_fails_the_zero_duration_count(self, data_dir, capsys):
        def drop_one(payload):
            for index, anomaly in enumerate(payload["anomalies"]):
                if anomaly["kind"] == "zero_duration":
                    payload["anomalies"].pop(index)
                    return
        self._rewrite(data_dir, "MB20261029", drop_one)
        assert run(data_dir) == 1
        assert "count zero_duration" in capsys.readouterr().out

    def test_flipping_an_audio_review_flag_fails(self, data_dir, capsys):
        def unflag(payload):
            for segment in payload["segments"]:
                if segment["speaker"]["needs_audio_review"]:
                    segment["speaker"]["needs_audio_review"] = False
                    return
        self._rewrite(data_dir, "MB20261028", unflag)
        assert run(data_dir) == 1
        assert "count audio_review" in capsys.readouterr().out


class TestIntegrityIsEnforced:
    def test_a_dangling_mention_fails(self, data_dir, capsys):
        path = data_dir / "canonical" / "MB20261028.canonical.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["mentions"].append(
            {"instrument_id": "stellantis", "segment_id": "MB20261028-999", "origin": "source_index"}
        )
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        # The canonical model rejects it on load, which is itself a validation failure.
        assert run(data_dir) == 1
        assert "FAIL" in capsys.readouterr().out

    def test_an_unregistered_speaker_fails(self, data_dir, capsys):
        path = data_dir / "registries" / "speakers.json"
        entries = [e for e in json.loads(path.read_text(encoding="utf-8"))
                   if e["speaker_id"] != "davide-biocchi"]
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        assert run(data_dir) == 1
        assert "speakers.json" in capsys.readouterr().out


class TestMissingInputs:
    def test_no_canonical_files_is_an_error(self, tmp_path):
        (tmp_path / "data" / "canonical").mkdir(parents=True)
        assert validate_data.main(["--data-dir", str(tmp_path / "data")]) == 1
