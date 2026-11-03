"""Shared test fixtures.

`scripts/` is not a package, so its path is added here to let tests exercise
the ingest script's registry merge directly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def en_v0_raw() -> dict:
    return load_fixture("en_v0_min.json")


@pytest.fixture
def it_v1_raw() -> dict:
    return load_fixture("it_v1_min.json")
