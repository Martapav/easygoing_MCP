"""Pydantic models of the registry files (section 4.4).

Registries are seeded by `scripts/ingest.py` and curated by hand afterwards.
Ingest only ever adds ids and aliases, so the curated fields — `role`,
`affiliation`, `links`, `disclosures`, `asset_class` — are the user's and stay
`None`/empty until someone fills them in.

Unknown fields are rejected. A typo in a curated key would otherwise be
accepted and silently ignored, leaving the curator believing they had set
something they had not.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Registry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SpeakerEntry(_Registry):
    speaker_id: str
    name: str
    aliases: list[str] = []

    # Curated by the user, never inferred from the transcripts (section 4.4).
    role: str | None = None
    affiliation: str | None = None
    links: list[str] = []
    disclosures: list[str] = []

    @property
    def is_curated(self) -> bool:
        """Whether a human has filled in anything beyond the seeded identity.

        Resources say so explicitly rather than presenting empty curation as
        fact (section 7, REQ-0011).
        """
        return bool(self.role or self.affiliation or self.links or self.disclosures)


class RubricEntry(_Registry):
    rubric_id: str
    name: str
    aliases: list[str] = []


class InstrumentEntry(_Registry):
    instrument_id: str
    name: str
    aliases: list[str] = []
    tickers: list[str] = []
    # Never guessed; stays null until curated (section 4.4).
    asset_class: str | None = None

    def match_terms(self) -> list[str]:
        """Everything this instrument can be referred to by (section 7, step 2)."""
        return [self.name, *self.aliases, *self.tickers]


class ProgrammeEntry(_Registry):
    programme_id: str
    name: str
