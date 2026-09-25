"""OMOP CDM v5.4 ``NOTE_NLP`` output (DESIGN_PLAN §8.2-8.3).

One CSV row per mention, with a header, in the v5.4 column order. Two inputs come from the
caller, never from UMLS:

- **``note_id``**: the integer ID of each document's ``NOTE`` row, from a mapping the caller
  supplies. It is never derived from a filename. A document with no ``note_id`` is refused
  (:class:`MissingNoteId`), not skipped silently.
- **OMOP concept IDs** need the OHDSI Standardized Vocabularies
  (:class:`cuiflow.terminology.omop.OmopVocabulary`). Without them both concept ID columns are 0,
  and the export's manifest says so.

**Which code a row carries.** A mention can have codes in several vocabularies; ``NOTE_NLP``
holds one concept. The writer takes the mention's *preferred* codes in the order
:data:`CODE_PRIORITY` (standard vocabularies first, ICD-10-CM last because its UMLS link is a
crosswalk) and uses the first one that maps to a standard concept, else the first one the
vocabulary knows at all. So concept IDs need the extraction run to have attached codes
(``code_systems``).

``section_concept_id`` is 0: there is no section → concept table yet (§8.2).

**``note_nlp_id``.** The v5.4 DDL declares it ``integer`` (32-bit in PostgreSQL), so the
default scheme, ``sequential``, numbers rows from ``first_id`` in results-file order: the same
results always get the same ids, and they load into a stock v5.4 schema. ``hash63`` is the
scheme DESIGN_PLAN §8.2 proposed, a 63-bit hash of ``(note_id, start, end, cui)`` that stays
stable across re-exports and runs; it needs the column widened to ``bigint``. (Hashing into 31
bits instead would collide: roughly a hundred collisions per million rows.) Either way the hash
input is the ``note_id``, never the document key, which may be a path or a patient identifier.

**The output is PHI** when snippets are on (they quote the note), and ``lexical_variant``
always quotes the mention.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from cuiflow import __version__
from cuiflow.core.models import DocumentResult, Mention
from cuiflow.terminology.omop import OmopVocabulary


def note_nlp_id(doc_key: str, start: int, end: int, cui: str) -> int:
    """A deterministic positive 63-bit id."""
    digest = hashlib.sha256(f"{doc_key}\x1f{start}\x1f{end}\x1f{cui}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


def term_exists(m: Mention) -> str | None:
    """OHDSI convention: does the patient actually have this?

    ``'N'`` if any assessed attribute rules it out (negated, not the patient, conditional,
    uncertain); ``NULL`` if negation was not assessed; otherwise ``'Y'``.
    """
    a = m.assertions
    if (
        a.negated is True
        or (a.subject is not None and a.subject != "patient")
        or a.conditional is True
        or a.uncertain is True
        or a.temporality == "hypothetical"
    ):
        return "N"
    if a.negated is None:
        return None
    return "Y"


def term_temporal(m: Mention) -> str | None:
    """``'past'`` / ``'present'`` from history_of (or ConText temporality); NULL if unassessed."""
    a = m.assertions
    if a.history_of is True or a.temporality == "historical":
        return "past"
    if a.history_of is False or a.temporality == "recent":
        return "present"
    return None


#: The v5.4 ``term_modifiers`` column is varchar(2000).
TERM_MODIFIERS_CHARS = 2000
_MODIFIER_VALUE_CHARS = 100  # at most 600 characters each once escaped


def term_modifiers(m: Mention) -> str:
    """The mention's assertions as JSON that fits ``term_modifiers``. A long free-text value
    is shortened, never the JSON, so the column always parses."""
    a = m.assertions
    text = json.dumps(
        {
            "cui": m.cui[:_MODIFIER_VALUE_CHARS],
            "negated": a.negated,
            "subject": a.subject[:_MODIFIER_VALUE_CHARS] if a.subject is not None else None,
            "uncertain": a.uncertain,
            "conditional": a.conditional,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert len(text) <= TERM_MODIFIERS_CHARS  # two short strings and three flags
    return text


#: The order in which a mention's preferred codes are tried for the row's concept.
CODE_PRIORITY: tuple[str, ...] = ("SNOMEDCT_US", "RXNORM", "LNC", "ICD10CM")

#: NOTE_NLP columns, CDM v5.4 order.
COLUMNS: tuple[str, ...] = (
    "note_nlp_id",
    "note_id",
    "section_concept_id",
    "snippet",
    "offset",
    "lexical_variant",
    "note_nlp_concept_id",
    "note_nlp_source_concept_id",
    "nlp_system",
    "nlp_date",
    "nlp_datetime",
    "term_exists",
    "term_temporal",
    "term_modifiers",
)
SNIPPET_CHARS = 250
#: ``note_nlp_id`` schemes (module docstring); ``integer`` in the v5.4 DDL is 32-bit.
ID_SCHEMES: tuple[str, ...] = ("sequential", "hash63")
INT32_MAX = 2**31 - 1


class MissingNoteId(LookupError):
    """A document has no ``note_id`` in the caller's mapping."""


def snippet(text: str, start: int, end: int, width: int = SNIPPET_CHARS) -> str:
    """Text around ``[start, end)``, at most ``width`` characters, the mention centred."""
    if end - start >= width:
        return text[start : start + width]
    pad = (width - (end - start)) // 2
    lo = max(0, start - pad)
    hi = min(len(text), lo + width)
    lo = max(0, hi - width)
    return text[lo:hi]


def choose_code(m: Mention) -> list[tuple[str, str]]:
    """The mention's preferred ``(system, code)`` pairs, in :data:`CODE_PRIORITY` order."""
    rank = {s: i for i, s in enumerate(CODE_PRIORITY)}
    pairs = [(c.system, c.code) for c in m.codes if c.is_preferred and c.system in rank]
    return sorted(pairs, key=lambda p: (rank[p[0]], p[1]))


class OmopNoteNlpWriter:
    """Streams ``NOTE_NLP`` rows to a CSV file. Refuses to overwrite one.

    ``note_ids`` maps a document key to its ``note_id``. ``nlp_datetime`` is the run's start
    (DESIGN_PLAN §8.2). ``snippets`` needs results that carry their text (``include_text``);
    without it the column is empty. ``id_scheme`` and ``first_id`` choose ``note_nlp_id``
    (module docstring).
    """

    def __init__(
        self,
        path: Path,
        *,
        note_ids: Callable[[str], int | None],
        nlp_datetime: datetime,
        vocabulary: OmopVocabulary | None = None,
        snippets: bool = False,
        id_scheme: str = "sequential",
        first_id: int = 1,
    ) -> None:
        if id_scheme not in ID_SCHEMES:
            raise ValueError(f"id_scheme must be one of {', '.join(ID_SCHEMES)}, not {id_scheme!r}")
        if not 1 <= first_id <= INT32_MAX:
            raise ValueError(f"first_id must be between 1 and {INT32_MAX}")
        if path.exists():
            raise FileExistsError(f"{path} exists")
        self.id_scheme = id_scheme
        self._next_id = first_id
        self.path = path
        self.note_ids = note_ids
        self.vocabulary = vocabulary
        self.snippets = snippets
        self.nlp_date = nlp_datetime.strftime("%Y-%m-%d")
        self.nlp_datetime = nlp_datetime.strftime("%Y-%m-%d %H:%M:%S")
        self.rows = 0
        self.rows_with_concept = 0
        self.snippets_missing = 0
        self._fh = path.open("x", encoding="utf-8", newline="")
        self._csv = csv.writer(self._fh, lineterminator="\n")
        self._csv.writerow(COLUMNS)

    def _concepts(self, m: Mention) -> tuple[int, int]:
        if self.vocabulary is None:
            return 0, 0
        known: tuple[int, int] | None = None
        for system, code in choose_code(m):
            found = self.vocabulary.lookup(system, code)
            if found.concept_id:  # maps to a standard concept: the best a row can carry
                return found.concept_id, found.source_concept_id
            if found.source_concept_id and known is None:
                known = (0, found.source_concept_id)
        return known or (0, 0)

    def write(self, result: DocumentResult) -> None:
        note_id = self.note_ids(result.doc_id)
        if note_id is None:
            raise MissingNoteId("document has no note_id in the mapping")
        meta = result.metadata
        system = f"cuiflow {__version__} {meta.get('mode', '')} {meta.get('profile', '')}".strip()
        doc_key = str(note_id)
        for m in result.mentions:
            concept_id, source_concept_id = self._concepts(m)
            if concept_id:
                self.rows_with_concept += 1
            snip = None
            if self.snippets:
                if result.text is not None:
                    snip = snippet(result.text, m.start, m.end)
                else:
                    self.snippets_missing += 1
            if self.id_scheme == "hash63":
                row_id = note_nlp_id(doc_key, m.start, m.end, m.cui)
            else:
                row_id = self._next_id
                if row_id > INT32_MAX:
                    raise ValueError(
                        "note_nlp_id passed the v5.4 integer range: lower first_id, or use "
                        "id_scheme='hash63' with a bigint note_nlp_id column"
                    )
                self._next_id += 1
            self._csv.writerow(
                (
                    row_id,
                    note_id,
                    0,
                    snip,
                    str(m.start),
                    m.text[:SNIPPET_CHARS],
                    concept_id,
                    source_concept_id,
                    system[:250],
                    self.nlp_date,
                    self.nlp_datetime,
                    term_exists(m),
                    term_temporal(m),
                    term_modifiers(m),
                )
            )
            self.rows += 1

    def close(self) -> None:
        self._fh.close()
