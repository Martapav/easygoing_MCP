"""Subject resolution: a subject string to the terms that find it (section 7).

Used by every tool that takes a `subject`. The ladder is section 7's:

1. normalize (casefold, strip accents, unify apostrophes, collapse whitespace);
2. match the instrument registry by name, alias or ticker;
3. otherwise fall back to a text match, marked `match_kind: "text"`;
4. an ambiguous registry match is an error listing the candidates, never a guess.

**Variants.** A subject is expanded into the surface forms that denote the same
thing, because one subject is spoken several ways: MB20261028 has Michettoni
saying "Fed" and Braganti saying "l'annuncio da parte della Federal Reserve"
about the same decision, and a resolver matching only the literal string finds
one of them. Two sources of variants, both deliberately narrow:

* **Registry-derived, not invented** — an instrument's names, aliases, tickers,
  and the parts of a name written as `A / B`. `Alphabet / Google` genuinely
  names one instrument two ways, so "Google" must reach it. All of this comes
  out of `instruments.json`.
* **Abbreviations** — a small table pairing an abbreviation with the full name
  of the *same* entity. This is the only hand-written part, so it is kept to
  pairs observed in these transcripts and reported in the output: resolution is
  never silent about having searched for something the user did not type.

This stays inside section 4.4's rule that there is no topics registry. Nothing
here creates a topic entity with an id; a subject is still resolved by matching
text, only against more than one spelling of itself.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from ..ingest.slugs import normalize
from ..models.registries import InstrumentEntry
from ..store.base import ContentStore, SegmentQuery

MatchKind = Literal["registry", "text"]

# Abbreviation <-> full form of the same entity, both directions.
#
# Only names of one entity belong here. FOMC is deliberately absent: the
# committee is not the bank, and folding them together would attribute a view
# about one to the other. Each pair below has at least one form occurring in
# the two ingested lives.
_ABBREVIATION_PAIRS: tuple[tuple[str, str], ...] = (
    ("fed", "federal reserve"),
    ("bce", "banca centrale europea"),
    ("boj", "bank of japan"),
    ("mib", "ftse mib"),
)


def _build_abbreviations() -> dict[str, tuple[str, ...]]:
    table: dict[str, set[str]] = defaultdict(set)
    for short, long in _ABBREVIATION_PAIRS:
        table[normalize(short)].add(normalize(long))
        table[normalize(long)].add(normalize(short))
    return {key: tuple(sorted(values)) for key, values in table.items()}


_ABBREVIATIONS = _build_abbreviations()


class SubjectError(ValueError):
    """Base class for subject resolution failures."""


class AmbiguousSubject(SubjectError):
    """The subject matches several registry instruments. Never guessed."""

    def __init__(self, subject: str, candidates: list[InstrumentEntry]) -> None:
        self.subject = subject
        self.candidates = candidates
        listed = ", ".join(f"{c.name!r} (instrument_id={c.instrument_id})" for c in candidates)
        super().__init__(
            f"subject {subject!r} matches several instruments: {listed}. "
            "Ask again with one of those names or instrument ids."
        )


class UnresolvableSubject(SubjectError):
    """The subject matches no instrument and occurs in no segment."""

    def __init__(self, subject: str, terms: tuple[str, ...], suggestions: list[str]) -> None:
        self.subject = subject
        self.terms = terms
        searched = ", ".join(repr(t) for t in terms)
        hint = (
            f" Closest known instruments: {', '.join(suggestions)}."
            if suggestions
            else " No instrument name resembles it."
        )
        super().__init__(
            f"subject {subject!r} was not found. Searched for {searched} in the instrument "
            f"registry and in segment text.{hint}"
        )


def _split_parts(name: str) -> list[str]:
    """`Alphabet / Google` -> the whole name plus each side.

    A slash in an instrument name separates two names for one instrument, so
    each side has to be searchable on its own.
    """
    parts = [part.strip() for part in name.split("/") if part.strip()]
    return [name, *parts] if len(parts) > 1 else [name]


def instrument_index(store: ContentStore) -> dict[str, list[str]]:
    """Normalized term -> instrument ids, built from the registry alone."""
    index: dict[str, set[str]] = defaultdict(set)
    for instrument in store.instruments():
        for term in instrument.match_terms():
            for form in _split_parts(term):
                normalized = normalize(form)
                if normalized:
                    index[normalized].add(instrument.instrument_id)
    return {term: sorted(ids) for term, ids in index.items()}


def expand_variants(subject: str) -> tuple[str, ...]:
    """Normalized surface forms to search for `subject`, the literal one first."""
    base = normalize(subject)
    variants = [base]
    for extra in _ABBREVIATIONS.get(base, ()):
        if extra not in variants:
            variants.append(extra)
    return tuple(variants)


@dataclass(frozen=True)
class ResolvedSubject:
    """What a subject resolved to, and how.

    `terms` is everything that will be searched; `variants_used` is the part of
    that the user did not type, which responses surface so the broadening is
    visible rather than silent.
    """

    subject: str
    match_kind: MatchKind
    terms: tuple[str, ...]
    variants_used: tuple[str, ...]
    instrument_ids: tuple[str, ...] = ()
    instrument: InstrumentEntry | None = None

    @property
    def label(self) -> str:
        """The name to show: the registry's, or the subject as asked."""
        return self.instrument.name if self.instrument is not None else self.subject


def resolve_subject(store: ContentStore, subject: str, *, verify: bool = True) -> ResolvedSubject:
    """Resolve a subject string against the registry, then against segment text.

    With `verify`, a subject that occurs nowhere raises `UnresolvableSubject`
    rather than returning a resolution that every caller would find empty
    (section 5: the error must let the model retry).
    """
    if not subject or not subject.strip():
        raise SubjectError("subject is empty")

    normalized = normalize(subject)
    index = instrument_index(store)

    # Step 2: the registry, by name, alias, ticker, or one side of `A / B`.
    matched_ids = index.get(normalized, [])
    if len(matched_ids) > 1:
        raise AmbiguousSubject(subject, [store.get_instrument(i) for i in matched_ids])

    if len(matched_ids) == 1:
        instrument = store.get_instrument(matched_ids[0])
        terms: list[str] = []
        for term in instrument.match_terms():
            for form in _split_parts(term):
                normalized_form = normalize(form)
                if normalized_form and normalized_form not in terms:
                    terms.append(normalized_form)
        # The asked-for form first, so it reads as the primary term.
        if normalized in terms:
            terms.remove(normalized)
        terms.insert(0, normalized)
        return ResolvedSubject(
            subject=subject,
            match_kind="registry",
            terms=tuple(terms),
            variants_used=tuple(t for t in terms if t != normalized),
            instrument_ids=(instrument.instrument_id,),
            instrument=instrument,
        )

    # Step 3: text fallback, with abbreviation variants.
    terms = list(expand_variants(subject))
    resolved = ResolvedSubject(
        subject=subject,
        match_kind="text",
        terms=tuple(terms),
        variants_used=tuple(t for t in terms if t != normalized),
    )

    if verify and store.search_segments(SegmentQuery(text_any=resolved.terms, limit=1)).total == 0:
        import difflib

        names = [instrument.name for instrument in store.instruments()]
        suggestions = difflib.get_close_matches(subject, names, n=3, cutoff=0.6)
        raise UnresolvableSubject(subject, resolved.terms, suggestions)

    return resolved


def subject_query(resolved: ResolvedSubject, **filters) -> SegmentQuery:
    """A `SegmentQuery` locating a resolved subject.

    For a registry subject both routes are passed: `instrument_ids` uses the
    source mention index and `text_any` the segment text. The store unions
    them, which is section 7's "mentions[] plus a text match".
    """
    return SegmentQuery(
        instrument_ids=resolved.instrument_ids or None,
        text_any=resolved.terms,
        **filters,
    )
