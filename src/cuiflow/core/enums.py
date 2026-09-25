"""Enumerations shared across the package."""

from __future__ import annotations

from enum import StrEnum


class EngineMode(StrEnum):
    """How documents are routed through the engines (DESIGN_PLAN §4.2)."""

    MMLITE = "single:mmlite"
    UMLSMATCH = "single:umlsmatch"
    UNION = "ensemble:union"  # measured: raises recall, costs too much precision
    CONSENSUS = "ensemble:consensus"  # measured: passes its gate; experimental
    HYBRID = "ensemble:hybrid_staged"  # needs umlsmatch>=0.2.0's assess() (DESIGN_PLAN §4.1)

    @property
    def engines(self) -> tuple[str, ...]:
        """The engine names this mode loads."""
        if self is EngineMode.MMLITE:
            return ("mmlite",)
        if self is EngineMode.UMLSMATCH:
            return ("umlsmatch",)
        return ("mmlite", "umlsmatch")

    @property
    def is_ensemble(self) -> bool:
        return self.value.startswith("ensemble:")


class OverlapPolicy(StrEnum):
    """What to do with overlapping mentions (DESIGN_PLAN §4.3)."""

    KEEP = "keep"
    LONGEST = "longest"


class ConflictPolicy(StrEnum):
    """How to combine engines' disagreeing assertions (DESIGN_PLAN §4.3)."""

    PREFER_UMLSMATCH = "prefer:umlsmatch"
    PREFER_MMLITE = "prefer:mmlite"
    NEGATED_IF_ANY = "negated_if_any"
    UNKNOWN = "unknown"


#: Vocabularies the terminology store resolves (UMLS SABs).
CODE_SYSTEMS: tuple[str, ...] = ("SNOMEDCT_US", "RXNORM", "ICD10CM", "LNC")

#: Systems whose CUI link is a crosswalk candidate rather than a coding decision.
CROSSWALK_SYSTEMS: frozenset[str] = frozenset({"ICD10CM"})
