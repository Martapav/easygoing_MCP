"""Text normalization and id slugging.

Section 4.1: speaker and rubric names become ids. Normalization unifies
typographic apostrophes and strips accents *before* slugging, so
`Chiediamolo all'AI` and `Chiediamolo all’AI` produce the same id.

The same `normalize` is reused by subject resolution (section 7), which is why
it lives apart from `slugify` rather than inside it.
"""

from __future__ import annotations

import re
import unicodedata

# Every codepoint we have seen (or might see) standing in for a typewriter apostrophe.
_APOSTROPHES = {
    "‘": "'",  # left single quotation mark
    "’": "'",  # right single quotation mark — the form used in both sources
    "ʼ": "'",  # modifier letter apostrophe
    "ʹ": "'",  # modifier letter prime
    "´": "'",  # acute accent used as apostrophe
    "`": "'",  # grave accent used as apostrophe
    "′": "'",  # prime
}

_APOSTROPHE_RE = re.compile("|".join(map(re.escape, _APOSTROPHES)))
_WHITESPACE_RE = re.compile(r"\s+")
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


def unify_apostrophes(value: str) -> str:
    """Map every apostrophe variant onto the plain ASCII `'`."""
    return _APOSTROPHE_RE.sub(lambda m: _APOSTROPHES[m.group()], value)


def strip_accents(value: str) -> str:
    """Decompose and drop combining marks, so `Agorà` becomes `Agora`."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(value: str) -> str:
    """Casefold, unify apostrophes, strip accents, collapse whitespace.

    This is the comparison form: two names that normalize alike are the same
    name. It keeps the apostrophe, so `dell'agora` stays distinguishable from
    `dellagora`; slugify is what discards punctuation.
    """
    text = unify_apostrophes(value)
    text = strip_accents(text)
    text = text.casefold()
    return _WHITESPACE_RE.sub(" ", text).strip()


def slugify(value: str) -> str:
    """Turn a display name into a stable id: `Davide Biocchi` -> `davide-biocchi`.

    Raises on input that has no slug-able content, rather than returning an
    empty id that would silently collide with another.
    """
    slug = _NON_SLUG_RE.sub("-", normalize(value)).strip("-")
    if not slug:
        raise ValueError(f"cannot slugify {value!r}: no alphanumeric content")
    return slug
