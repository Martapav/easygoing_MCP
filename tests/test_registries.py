"""Registry seeding and merge (sections 4.4 and 9).

The merge has to be safe to re-run: a new live must add ids and aliases without
touching anything a human curated, because curation is the only source of
roles, affiliations and asset classes.
"""

from __future__ import annotations

import json

import pytest

from easygoing_mcp.config import Settings

from ingest import Report, Seed, merge_registries, split_tickers  # noqa: E402  (scripts/ on sys.path)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path)


def read(settings: Settings, name: str) -> list[dict]:
    return json.loads((settings.registries_dir / name).read_text(encoding="utf-8"))


def seed_one() -> Seed:
    seed = Seed()
    seed.speakers["ada-lovelace"] = "Ada Lovelace"
    seed.rubrics["grafico-del-giorno"] = "Grafico del giorno"
    seed.programmes["market-briefing"] = "Market Briefing"
    seed.instruments["sk-hynix"] = ("SK hynix", "000660 (KRX)")
    return seed


class TestSplitTickers:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("GOOGL / GOOG", ["GOOGL", "GOOG"]),
            ("GOOGL/GOOG", ["GOOGL", "GOOG"]),  # both spellings converge
            ("STLAM", ["STLAM"]),
            ("000660 (KRX)", ["000660 (KRX)"]),  # exchange suffix preserved verbatim
            (None, []),
            ("", []),
        ],
    )
    def test_splits_on_slash_only(self, raw, expected):
        assert split_tickers(raw) == expected


class TestSeeding:
    def test_creates_entries_with_uncurated_fields_null(self, settings):
        merge_registries([seed_one()], settings, Report(), dry_run=False)

        speaker = read(settings, "speakers.json")[0]
        assert speaker["speaker_id"] == "ada-lovelace"
        # Never filled from the transcripts; these come from the user (section 4.4).
        assert speaker["role"] is None
        assert speaker["affiliation"] is None
        assert speaker["disclosures"] == []

        instrument = read(settings, "instruments.json")[0]
        assert instrument["asset_class"] is None  # never guessed
        assert instrument["tickers"] == ["000660 (KRX)"]

    def test_dry_run_writes_nothing(self, settings):
        merge_registries([seed_one()], settings, Report(), dry_run=True)
        assert not (settings.registries_dir / "speakers.json").exists()

    def test_no_topics_registry_is_created(self, settings):
        merge_registries([seed_one()], settings, Report(), dry_run=False)
        assert not (settings.registries_dir / "topics.json").exists()


class TestMergePreservesCuration:
    def test_curated_fields_survive_reingestion(self, settings):
        merge_registries([seed_one()], settings, Report(), dry_run=False)

        # A human curates the registry between ingests.
        path = settings.registries_dir / "speakers.json"
        entries = json.loads(path.read_text(encoding="utf-8"))
        entries[0]["role"] = "Host"
        entries[0]["affiliation"] = "EasyGoing"
        entries[0]["aliases"].append("Ada")
        entries[0]["disclosures"] = ["holds no positions"]
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

        # Re-ingesting the same live must not undo any of it.
        merge_registries([seed_one()], settings, Report(), dry_run=False)

        after = read(settings, "speakers.json")[0]
        assert after["role"] == "Host"
        assert after["affiliation"] == "EasyGoing"
        assert after["disclosures"] == ["holds no positions"]
        assert "Ada" in after["aliases"]

    def test_curated_asset_class_survives(self, settings):
        merge_registries([seed_one()], settings, Report(), dry_run=False)
        path = settings.registries_dir / "instruments.json"
        entries = json.loads(path.read_text(encoding="utf-8"))
        entries[0]["asset_class"] = "equity"
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

        merge_registries([seed_one()], settings, Report(), dry_run=False)
        assert read(settings, "instruments.json")[0]["asset_class"] == "equity"

    def test_a_new_name_becomes_an_alias_not_an_overwrite(self, settings):
        merge_registries([seed_one()], settings, Report(), dry_run=False)

        renamed = Seed()
        renamed.instruments["sk-hynix"] = ("SK Hynix Inc.", "000660 (KRX)")
        merge_registries([renamed], settings, Report(), dry_run=False)

        entry = read(settings, "instruments.json")[0]
        assert entry["name"] == "SK hynix"           # original name kept
        assert "SK Hynix Inc." in entry["aliases"]   # the variant is recorded

    def test_merge_is_idempotent(self, settings):
        merge_registries([seed_one()], settings, Report(), dry_run=False)
        first = (settings.registries_dir / "instruments.json").read_text(encoding="utf-8")
        merge_registries([seed_one()], settings, Report(), dry_run=False)
        assert (settings.registries_dir / "instruments.json").read_text(encoding="utf-8") == first


class TestTickerConflicts:
    def test_missing_ticker_in_one_live_is_flagged_and_both_kept(self, settings):
        with_ticker = seed_one()
        without_ticker = Seed()
        without_ticker.instruments["sk-hynix"] = ("SK hynix", None)

        report = Report()
        merge_registries([with_ticker, without_ticker], settings, report, dry_run=False)

        assert read(settings, "instruments.json")[0]["tickers"] == ["000660 (KRX)"]
        assert any("ticker missing" in warning for warning in report.warnings)

    def test_conflicting_tickers_are_both_kept_and_flagged(self, settings):
        first = Seed()
        first.instruments["acme"] = ("Acme", "ACME")
        second = Seed()
        second.instruments["acme"] = ("Acme", "ACM")

        report = Report()
        merge_registries([first, second], settings, report, dry_run=False)

        # Kept side by side rather than resolved automatically (section 4.4).
        assert read(settings, "instruments.json")[0]["tickers"] == ["ACME", "ACM"]
        assert any("ticker conflict" in warning for warning in report.warnings)

    def test_spacing_variants_do_not_count_as_a_conflict(self, settings):
        spaced = Seed()
        spaced.instruments["alphabet-google"] = ("Alphabet / Google", "GOOGL / GOOG")
        tight = Seed()
        tight.instruments["alphabet-google"] = ("Alphabet / Google", "GOOGL/GOOG")

        report = Report()
        merge_registries([spaced, tight], settings, report, dry_run=False)

        assert read(settings, "instruments.json")[0]["tickers"] == ["GOOGL", "GOOG"]
        assert not [w for w in report.warnings if "alphabet-google" in w]
