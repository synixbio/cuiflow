"""The canonical, versioned data model every engine and interface produces (DESIGN_PLAN §3).

**An assertion attribute that was not assessed is ``None``, never ``False``.** ``False`` claims an
assessment nobody made, and downstream users will read it as one. Every adapter and writer must
preserve that distinction.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, Literal

SCHEMA_VERSION = "1"

Subject = Literal["patient", "family_member", "other"]
Temporality = Literal["recent", "historical", "hypothetical"]

#: The AssertionStatus attribute names, in output order.
ASSERTION_ATTRIBUTES: tuple[str, ...] = (
    "negated",
    "subject",
    "history_of",
    "uncertain",
    "conditional",
    "generic",
    "temporality",
)


def _freeze(mapping: Mapping[str, Any]) -> Mapping[str, Any]:
    return mapping if isinstance(mapping, MappingProxyType) else MappingProxyType(dict(mapping))


def make_mention_id(doc_id: str, start: int, end: int, cui: str) -> str:
    """A stable mention identifier that does not embed ``doc_id`` (which may be PHI)."""
    digest = hashlib.sha256(f"{doc_id}\x1f{start}\x1f{end}\x1f{cui}".encode()).hexdigest()
    return digest[:16]


@dataclass(frozen=True, slots=True)
class TerminologyCode:
    """A code in an external vocabulary linked to a CUI through MRCONSO."""

    system: str  # UMLS SAB: "SNOMEDCT_US", "RXNORM", "ICD10CM", "LNC"
    code: str
    tty: str  # term type: "PT", "FN", "IN", "SCD", …
    display: str
    is_preferred: bool  # the canonical code for this CUI in this system (DESIGN_PLAN §5.2)
    is_crosswalk: bool = False  # True when the link is UMLS co-occurrence, not a coding decision
    # Filled only on request; empty means "not looked up", not "no ancestors".
    ancestors: tuple[str, ...] = ()
    ingredients: tuple[str, ...] = ()  # RxNorm products only: ingredient RxCUIs (TTY IN/PIN)

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "code": self.code,
            "tty": self.tty,
            "display": self.display,
            "is_preferred": self.is_preferred,
            "is_crosswalk": self.is_crosswalk,
            "ancestors": list(self.ancestors),
            "ingredients": list(self.ingredients),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> TerminologyCode:
        return cls(
            system=d["system"],
            code=d["code"],
            tty=d["tty"],
            display=d["display"],
            is_preferred=d["is_preferred"],
            is_crosswalk=d.get("is_crosswalk", False),
            ancestors=tuple(d.get("ancestors", ())),
            ingredients=tuple(d.get("ingredients", ())),
        )


@dataclass(frozen=True, slots=True)
class AssertionStatus:
    """Clinical assertion attributes. ``None`` means not assessed."""

    negated: bool | None = None
    subject: Subject | None = None
    history_of: bool | None = None
    uncertain: bool | None = None
    conditional: bool | None = None
    generic: bool | None = None
    temporality: Temporality | None = None  # ConText values

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in ASSERTION_ATTRIBUTES}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> AssertionStatus:
        return cls(**{name: d.get(name) for name in ASSERTION_ATTRIBUTES})

    def assessed(self) -> frozenset[str]:
        """Names of the attributes that carry an assessment (are not ``None``)."""
        return frozenset(n for n in ASSERTION_ATTRIBUTES if getattr(self, n) is not None)


@dataclass(frozen=True, slots=True)
class ProvenanceInfo:
    """Which engines found a mention, and what each said about it."""

    found_by: tuple[str, ...]  # ("mmlite",), ("umlsmatch",) or both
    semantic_types: tuple[str, ...] = ()  # TUIs, e.g. ("T047",)
    matched_term: str = ""  # the dictionary string that matched
    # Per-engine assertions, recorded when engines disagree (DESIGN_PLAN §4.3).
    engine_assertions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict, hash=False)
    assertion_conflict: bool = False
    #: The engine whose rules set the assertions, when it is not the one in ``found_by``
    #: (``ensemble:hybrid_staged``: found by mmlite, assessed by umlsmatch). None: the
    #: assertions come from the ``found_by`` engines themselves.
    assessed_by: str | None = None

    def __post_init__(self) -> None:
        frozen = {k: _freeze(v) for k, v in self.engine_assertions.items()}
        object.__setattr__(self, "engine_assertions", MappingProxyType(frozen))

    def to_dict(self) -> dict[str, Any]:
        return {
            "found_by": list(self.found_by),
            "semantic_types": list(self.semantic_types),
            "matched_term": self.matched_term,
            "engine_assertions": {k: dict(v) for k, v in self.engine_assertions.items()},
            "assertion_conflict": self.assertion_conflict,
            "assessed_by": self.assessed_by,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ProvenanceInfo:
        return cls(
            found_by=tuple(d.get("found_by", ())),
            semantic_types=tuple(d.get("semantic_types", ())),
            matched_term=d.get("matched_term", ""),
            engine_assertions=d.get("engine_assertions", {}),
            assertion_conflict=d.get("assertion_conflict", False),
            assessed_by=d.get("assessed_by"),
        )


@dataclass(frozen=True, slots=True)
class Mention:
    """One concept (CUI) found at one span of a document."""

    mention_id: str  # make_mention_id(): a hash, never the raw doc_id
    start: int  # character offset, inclusive
    end: int  # character offset, exclusive
    text: str  # document[start:end]
    cui: str
    preferred_name: str
    semantic_group: str  # cTAKES group: "DISORDER", "DRUG", "PROCEDURE", "ANATOMY", "FINDING", …
    assertions: AssertionStatus
    section: str | None = None  # normalized section name, when an engine detected one
    codes: tuple[TerminologyCode, ...] = ()
    provenance: ProvenanceInfo = field(default_factory=lambda: ProvenanceInfo(found_by=()))

    def __post_init__(self) -> None:
        if not 0 <= self.start <= self.end:
            raise ValueError(f"invalid span [{self.start}, {self.end})")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mention_id": self.mention_id,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "cui": self.cui,
            "preferred_name": self.preferred_name,
            "semantic_group": self.semantic_group,
            "assertions": self.assertions.to_dict(),
            "section": self.section,
            "codes": [c.to_dict() for c in self.codes],
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Mention:
        return cls(
            mention_id=d["mention_id"],
            start=d["start"],
            end=d["end"],
            text=d["text"],
            cui=d["cui"],
            preferred_name=d["preferred_name"],
            semantic_group=d["semantic_group"],
            assertions=AssertionStatus.from_dict(d.get("assertions", {})),
            section=d.get("section"),
            codes=tuple(TerminologyCode.from_dict(c) for c in d.get("codes", ())),
            provenance=ProvenanceInfo.from_dict(d.get("provenance", {})),
        )


@dataclass(frozen=True, slots=True)
class SectionSpan:
    """A detected section heading."""

    header: str  # normalized, e.g. "PAST_MEDICAL_HISTORY"
    raw_header: str  # as written, e.g. "Past Medical History:"
    start: int
    end: int  # end of the heading; the section runs to the next heading

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> SectionSpan:
        return cls(header=d["header"], raw_header=d["raw_header"], start=d["start"], end=d["end"])


@dataclass(frozen=True, slots=True)
class DocumentResult:
    """The complete result for one document."""

    doc_id: str
    mentions: tuple[Mention, ...]
    sections: tuple[SectionSpan, ...] = ()
    text: str | None = None  # included only when requested: it is the note itself (PHI)
    schema_version: str = SCHEMA_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict, hash=False)  # engines, timing

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dictionary."""
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "doc_id": self.doc_id,
            "mentions": [m.to_dict() for m in self.mentions],
            "sections": [s.to_dict() for s in self.sections],
            "metadata": dict(self.metadata),
        }
        if self.text is not None:
            out["text"] = self.text
        return out

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> DocumentResult:
        version = d.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {version!r}; expected {SCHEMA_VERSION}")
        return cls(
            doc_id=d["doc_id"],
            mentions=tuple(Mention.from_dict(m) for m in d.get("mentions", ())),
            sections=tuple(SectionSpan.from_dict(s) for s in d.get("sections", ())),
            text=d.get("text"),
            schema_version=version,
            metadata=d.get("metadata", {}),
        )
