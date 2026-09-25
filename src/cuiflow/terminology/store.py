"""Read access to ``terminology.sqlite`` (DESIGN_PLAN §5.1).

The database is built once by :mod:`cuiflow.terminology.builder` from the user's own licensed
UMLS files, opened read-only here, and never distributed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

from cuiflow.core.enums import CODE_SYSTEMS, CROSSWALK_SYSTEMS
from cuiflow.core.models import TerminologyCode

#: Sources in the order their names are used for a concept: standard vocabularies first,
#: ICD-10-CM (linked to UMLS concepts by crosswalk) last. The OMOP writer ranks codes the same way.
DISPLAY_PRIORITY: tuple[str, ...] = ("SNOMEDCT_US", "RXNORM", "LNC", "ICD10CM")

SCHEMA = """
CREATE TABLE IF NOT EXISTS build_info (
    umls_release TEXT,
    built_at TEXT NOT NULL,
    sources TEXT NOT NULL,
    include_suppressed INTEGER NOT NULL,
    cuiflow_version TEXT NOT NULL,
    hierarchy_sources TEXT NOT NULL DEFAULT '',  -- SABs with code_hierarchy rows
    hierarchy_max_depth INTEGER,                 -- NULL: walked to the roots
    rxnorm_ingredients INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cui_codes (
    cui TEXT NOT NULL,
    sab TEXT NOT NULL,
    code TEXT NOT NULL,
    aui TEXT NOT NULL,
    tty TEXT NOT NULL,
    -- One string per (code, term type): the first MRCONSO row wins, so a code with several
    -- synonyms (tty 'SY') keeps only one. Codes are complete; use str as a label, not a
    -- synonym list.
    str TEXT NOT NULL,
    is_preferred INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (cui, sab, code, tty)
);

CREATE TABLE IF NOT EXISTS code_hierarchy (
    sab TEXT NOT NULL,
    code TEXT NOT NULL,
    ancestor_code TEXT NOT NULL,
    distance INTEGER NOT NULL,
    PRIMARY KEY (sab, code, ancestor_code)
);

CREATE TABLE IF NOT EXISTS rxnorm_ingredients (
    product_cui TEXT NOT NULL,
    ingredient_cui TEXT NOT NULL,
    ingredient_rxcui TEXT NOT NULL,
    ingredient_name TEXT NOT NULL,
    hops INTEGER NOT NULL,
    PRIMARY KEY (product_cui, ingredient_cui)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS ix_cui_codes_cui ON cui_codes(cui);
CREATE INDEX IF NOT EXISTS ix_cui_codes_sab_code ON cui_codes(sab, code);
CREATE INDEX IF NOT EXISTS ix_hierarchy_lookup ON code_hierarchy(sab, code);
CREATE INDEX IF NOT EXISTS ix_hierarchy_ancestor ON code_hierarchy(sab, ancestor_code);
CREATE INDEX IF NOT EXISTS ix_ingredients_product ON rxnorm_ingredients(product_cui);
"""


class Ingredient(NamedTuple):
    cui: str
    rxcui: str
    name: str
    hops: int  # 0 when the product is itself the ingredient


class TerminologyStore:
    """Resolves CUIs to codes. Not thread-safe: one per thread or process."""

    def __init__(self, path: Path | str, *, cache_size: int = 50_000) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(
                f"terminology database not found: {self.path} "
                "(build one with: cuiflow build-terminology)"
            )
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._codes = lru_cache(maxsize=cache_size)(self._codes_uncached)
        self._info = self._read_build_info()

    def _read_build_info(self) -> dict[str, Any]:
        # SELECT * so a store built before a column was added still opens.
        cur = self._conn.execute("SELECT * FROM build_info LIMIT 1")
        row = cur.fetchone()
        if row is None:
            return {}
        raw = dict(zip([d[0] for d in cur.description], row, strict=True))
        hier = raw.get("hierarchy_sources") or ""
        return {
            "path": str(self.path),
            "umls_release": raw["umls_release"],
            "built_at": raw["built_at"],
            "sources": raw["sources"].split(",") if raw["sources"] else [],
            "include_suppressed": bool(raw["include_suppressed"]),
            "cuiflow_version": raw["cuiflow_version"],
            "hierarchy_sources": hier.split(",") if hier else [],
            "hierarchy_max_depth": raw.get("hierarchy_max_depth"),
            "rxnorm_ingredients": bool(raw.get("rxnorm_ingredients", 0)),
        }

    def build_info(self) -> dict[str, Any]:
        return dict(self._info)

    @property
    def hierarchy_sources(self) -> frozenset[str]:
        """Sources whose ancestors this store holds (empty: built without MRREL)."""
        return frozenset(self._info.get("hierarchy_sources", ()))

    @property
    def has_ingredients(self) -> bool:
        return bool(self._info.get("rxnorm_ingredients"))

    def _codes_uncached(
        self, cui: str, systems: tuple[str, ...], ancestor_depth: int, ingredients: bool
    ) -> tuple[TerminologyCode, ...]:
        marks = ",".join("?" * len(systems))
        rows = self._conn.execute(
            f"SELECT sab, code, tty, str, is_preferred FROM cui_codes "
            f"WHERE cui = ? AND sab IN ({marks}) "
            f"ORDER BY sab, is_preferred DESC, code, tty",
            (cui, *systems),
        ).fetchall()
        ingredient_codes: tuple[str, ...] = ()
        if ingredients and any(r[0] == "RXNORM" for r in rows):
            ingredient_codes = tuple(i.rxcui for i in self.ingredients(cui))
        return tuple(
            TerminologyCode(
                system=sab,
                code=code,
                tty=tty,
                display=display,
                is_preferred=bool(pref),
                is_crosswalk=sab in CROSSWALK_SYSTEMS,
                ancestors=(
                    tuple(a for a, _ in self.ancestors(sab, code, max_distance=ancestor_depth))
                    if ancestor_depth and sab in self.hierarchy_sources
                    else ()
                ),
                ingredients=ingredient_codes if sab == "RXNORM" else (),
            )
            for sab, code, tty, display, pref in rows
        )

    def codes_for(
        self,
        cui: str,
        systems: Iterable[str] = CODE_SYSTEMS,
        *,
        preferred_only: bool = False,
        ancestor_depth: int = 0,
        ingredients: bool = False,
    ) -> tuple[TerminologyCode, ...]:
        """Codes for ``cui``, preferred first within each system.

        ``ancestor_depth`` > 0 fills each ICD-10-CM / SNOMED CT code's ``ancestors`` (nearest
        first, up to that distance); ``ingredients`` fills each RxNorm code's ingredient RxCUIs.
        Both need a store built with MRREL (:meth:`require`).
        """
        codes: tuple[TerminologyCode, ...] = self._codes(
            cui, tuple(sorted(set(systems))), ancestor_depth, ingredients
        )
        return tuple(c for c in codes if c.is_preferred) if preferred_only else codes

    def require(self, *, hierarchy: bool = False, ingredients: bool = False) -> None:
        """Raise ``ValueError`` if this store lacks what a configuration asks for."""
        rebuild = "rebuild it with: cuiflow build-terminology --mrrel <META>/MRREL.RRF"
        if hierarchy and not self.hierarchy_sources:
            raise ValueError(f"{self.path} has no code hierarchy; {rebuild}")
        if ingredients and not self.has_ingredients:
            raise ValueError(f"{self.path} has no RxNorm ingredients; {rebuild}")

    def cuis_for_code(self, system: str, code: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT cui FROM cui_codes WHERE sab = ? AND code = ? ORDER BY cui",
            (system, code),
        ).fetchall()
        return [r[0] for r in rows]

    def preferred_display(self, cui: str) -> str | None:
        """A display name for a CUI from the store's own rows: a preferred string, from the
        first source in :data:`DISPLAY_PRIORITY` that has one (ICD-10-CM's crosswalk wording
        last), then any string."""
        rank = " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(DISPLAY_PRIORITY))
        row = self._conn.execute(
            "SELECT str FROM cui_codes WHERE cui = ? "
            f"ORDER BY is_preferred DESC, CASE sab {rank} ELSE {len(DISPLAY_PRIORITY)} END, "
            "sab, code LIMIT 1",
            (cui,),
        ).fetchone()
        return row[0] if row else None

    def ancestors(
        self, system: str, code: str, *, max_distance: int | None = None
    ) -> list[tuple[str, int]]:
        """``(ancestor_code, distance)``, nearest first; 1 is a parent.

        Empty for a top-level code, and for every code in a store built without MRREL (check
        :attr:`hierarchy_sources`).
        """
        rows = self._conn.execute(
            "SELECT ancestor_code, distance FROM code_hierarchy "
            "WHERE sab = ? AND code = ? AND distance <= ? ORDER BY distance, ancestor_code",
            (system, code, max_distance if max_distance is not None else 1 << 30),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def descendants(
        self, system: str, code: str, *, max_distance: int | None = None
    ) -> list[tuple[str, int]]:
        """``(descendant_code, distance)``, nearest first: the codes ``code`` is an ancestor of.

        Empty for a leaf code and in a store built without MRREL. A store built with
        ``--hierarchy-max-depth N`` holds no descendant more than N levels down.
        """
        rows = self._conn.execute(
            "SELECT code, distance FROM code_hierarchy "
            "WHERE sab = ? AND ancestor_code = ? AND distance <= ? ORDER BY distance, code",
            (system, code, max_distance if max_distance is not None else 1 << 30),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def ingredients(self, cui: str) -> list[Ingredient]:
        """The RxNorm ingredients (TTY IN/PIN) a product CUI resolves to; ``hops`` 0 for itself.

        Empty when the CUI does not resolve, and in a store built without MRREL (check
        :attr:`has_ingredients`).
        """
        rows = self._conn.execute(
            "SELECT ingredient_cui, ingredient_rxcui, ingredient_name, hops "
            "FROM rxnorm_ingredients WHERE product_cui = ? ORDER BY hops, ingredient_name",
            (cui,),
        ).fetchall()
        return [Ingredient(*r) for r in rows]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> TerminologyStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
