"""Pydantic models of the RAW `mb_mymeeting_it_v1` format.

Source: Market_Briefing_2026-10-29_MCP_TEST.json, which labels itself
`easygoing.market_briefing.transcript_test.v1`.

As with the en_v0 models these mirror the source exactly, Italian key names
included, and forbid unknown fields.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ItV1Chapter(_Strict):
    nome: str
    """Rubric name or guest name; classified against `speaker_ufficiali`."""

    timestamp_youtube_ufficiale: str
    """May carry an annotation, e.g. `25:02 [da verificare]`."""

    timestamp_registrazione_inizio_approssimato: str
    tema: str


class ItV1Instrument(_Strict):
    nome: str
    ticker: str | None
    """Null for 12 of 34 entries (indices, commodities, some equities)."""

    occorrenze_segmenti: int
    speaker: dict[str, int]
    """Per-speaker mention counts. Recomputed and cross-checked, not stored."""

    prima_occorrenza_rec: str
    prima_occorrenza_youtube_stimata: str
    segmenti_ids: list[int]
    """Raw integer segment ids, mapped to canonical ids during adaptation."""


class ItV1Segment(_Strict):
    id: int
    """0..240 in this file; formatted as `{live_id}-{id:03d}`."""

    time_recording: str
    time_recording_seconds: int
    time_youtube_estimated: str
    time_youtube_estimated_seconds: int
    """Interpolated between chapter anchors, not measured."""

    speaker: str
    speaker_originale: str
    """The raw diarization label, before reattribution."""

    speaker_confidence: str
    requires_audio_check: bool
    rubrica: str
    """Mixes rubric names and guest names; matches a `capitoli_ufficiali.nome`."""

    testo: str


class ItV1Document(_Strict):
    schema_: str = Field(alias="schema")
    """Format discriminator. Aliased because `schema` shadows a BaseModel member."""

    data: str
    titolo: str
    programma: str
    origine: str
    registrazione_durata_dichiarata_minuti: int
    prima_voce_pubblica_rec: str
    ultima_voce_disponibile_rec: str
    contenuto_incompleto: bool
    tempo_youtube_segmenti: str
    limiti: list[str]
    """Each item also becomes a live-level `source_limitation` anomaly."""

    speaker_ufficiali: list[str]
    capitoli_ufficiali: list[ItV1Chapter]
    titoli_e_strumenti: list[ItV1Instrument]
    segmenti: list[ItV1Segment] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
