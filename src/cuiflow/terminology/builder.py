"""Build ``terminology.sqlite`` from licensed UMLS files (DESIGN_PLAN §5).

- ``MRCONSO.RRF`` → CUI → code rows, with the preferred-code rule (``MRRANK.RRF`` as fallback).
- ``MRREL.RRF`` (optional) → the code-level hierarchy for ICD-10-CM and SNOMED CT
  (:mod:`cuiflow.terminology.hierarchy`) and RxNorm ingredients
  (:mod:`cuiflow.terminology.ingredients`).

Both files are streamed, never loaded whole: they are several GB. MRREL is read first and only
the relation rows the walks need are kept (CHD rows for the hierarchy sources, three RxNorm
RELAs), so MRCONSO can then keep just the atoms those relations mention.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from cuiflow.core.enums import CODE_SYSTEMS
from cuiflow.core.manifests import utc_now
from cuiflow.terminology.hierarchy import HIERARCHY_SOURCES, code_ancestors
from cuiflow.terminology.ingredients import (
    BREAKDOWN_RELAS,
    INGREDIENT_TTYS,
    resolve_ingredients,
)
from cuiflow.terminology.store import INDEXES, SCHEMA

# MRCONSO.RRF: CUI|LAT|TS|LUI|STT|SUI|ISPREF|AUI|SAUI|SCUI|SDUI|SAB|TTY|CODE|STR|SRL|SUPPRESS|CVF
_CUI, _LAT, _AUI, _SAB, _TTY, _CODE, _STR, _SUPPRESS = 0, 1, 7, 11, 12, 13, 14, 16
# MRREL.RRF: CUI1|AUI1|STYPE1|REL|CUI2|AUI2|STYPE2|RELA|RUI|SRUI|SAB|SL|RG|DIR|SUPPRESS|CVF
_R_CUI1, _R_AUI1, _R_REL, _R_CUI2, _R_AUI2, _R_RELA, _R_SAB, _R_SUPPRESS = 0, 1, 3, 4, 5, 7, 10, 14
# MRRANK.RRF: RANK|SAB|TTY|SUPPRESS
_SUPPRESSED = frozenset({"O", "E", "Y"})
_RELEASE = re.compile(r"(?<!\d)(\d{4}A[AB])(?![A-Za-z0-9])")
_BATCH = 50_000

Progress = Callable[[str, int], None]


def guess_release(path: Path) -> str | None:
    """A UMLS release name (e.g. '2026AA') found in the file's path, if any."""
    m = _RELEASE.search(str(path.resolve()))
    return m.group(1) if m else None


def _mrconso_rows(
    path: Path, sources: frozenset[str], include_suppressed: bool
) -> Iterable[tuple[str, str, str, str, str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        for line in fh:
            f = line.rstrip("\r\n").split("|")
            if len(f) < 18 or f[_SAB] not in sources or f[_LAT] != "ENG":
                continue
            if not include_suppressed and f[_SUPPRESS] in _SUPPRESSED:
                continue
            yield f[_CUI], f[_SAB], f[_CODE], f[_AUI], f[_TTY], f[_STR]


def _mrrank_rows(path: Path, sources: frozenset[str]) -> Iterable[tuple[int, str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        for line in fh:
            f = line.rstrip("\r\n").split("|")
            if len(f) >= 3 and f[1] in sources:
                yield int(f[0]), f[1], f[2]


@dataclass
class Relations:
    """The MRREL rows the walks need, already turned the right way round."""

    #: sab -> child AUI -> parent AUIs, from CHD rows (parent in AUI1, child in AUI2).
    chd: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    #: coarser RxNorm CUI -> finer CUIs ("CUI2 RELA CUI1", so graph[CUI2].add(CUI1)).
    rxnorm: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    rows_read: int = 0

    def atoms(self, sab: str) -> set[str]:
        graph = self.chd.get(sab, {})
        out = set(graph)
        for parents in graph.values():
            out.update(parents)
        return out


def read_relations(
    mrrel: Path,
    hierarchy_sources: Iterable[str],
    *,
    rxnorm: bool,
    include_suppressed: bool = False,
    progress: Progress | None = None,
) -> Relations:
    """One streaming pass over MRREL, keeping only what the hierarchy and ingredient walks use."""
    wanted = frozenset(hierarchy_sources)
    rel = Relations(chd={sab: defaultdict(set) for sab in wanted})
    with mrrel.open(encoding="utf-8", errors="replace", newline="") as fh:
        for line in fh:
            rel.rows_read += 1
            if progress and rel.rows_read % 1_000_000 == 0:
                progress("MRREL", rel.rows_read)
            f = line.rstrip("\r\n").split("|")
            if len(f) <= _R_SUPPRESS:
                continue
            if not include_suppressed and f[_R_SUPPRESS] in _SUPPRESSED:
                continue
            sab = f[_R_SAB]
            if sab in wanted and f[_R_REL] == "CHD":
                parent, child = f[_R_AUI1], f[_R_AUI2]
                if parent and child and parent != child:
                    rel.chd[sab][child].add(parent)
            elif rxnorm and sab == "RXNORM" and f[_R_RELA] in BREAKDOWN_RELAS:
                coarser, finer = f[_R_CUI2], f[_R_CUI1]
                if coarser != finer:
                    rel.rxnorm[coarser].add(finer)
    return rel


def _insert_batched(conn: sqlite3.Connection, sql: str, rows: Iterable[tuple[object, ...]]) -> int:
    n = 0
    batch: list[tuple[object, ...]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= _BATCH:
            conn.executemany(sql, batch)
            n += len(batch)
            batch.clear()
    conn.executemany(sql, batch)
    return n + len(batch)


def build_terminology(
    mrconso: Path,
    mrrank: Path,
    out: Path,
    *,
    mrrel: Path | None = None,
    sources: Iterable[str] = CODE_SYSTEMS,
    include_suppressed: bool = False,
    hierarchy_max_depth: int | None = None,
    umls_release: str | None = None,
    progress: Progress | None = None,
) -> Path:
    """Build a fresh terminology database at ``out``; refuses to overwrite an existing file.

    Without ``mrrel`` the store has codes only: no hierarchy, no ingredients. With it,
    ``hierarchy_max_depth`` caps the ancestor walk (``None`` walks to the roots).
    """
    from cuiflow import __version__

    if out.exists():
        raise FileExistsError(f"{out} exists; remove it first or choose another path")
    if hierarchy_max_depth is not None and hierarchy_max_depth < 1:
        raise ValueError(
            f"hierarchy_max_depth must be at least 1 (omit it for the full chain), "
            f"got {hierarchy_max_depth}"
        )
    wanted = frozenset(sources)
    hier_sabs = tuple(s for s in HIERARCHY_SOURCES if s in wanted) if mrrel else ()
    want_rxnorm = mrrel is not None and "RXNORM" in wanted
    release = umls_release or guess_release(mrconso)

    relations = (
        read_relations(
            mrrel,
            hier_sabs,
            rxnorm=want_rxnorm,
            include_suppressed=include_suppressed,
            progress=progress,
        )
        if mrrel is not None
        else Relations()
    )
    graph_atoms = {sab: relations.atoms(sab) for sab in hier_sabs}
    atom_code: dict[str, dict[str, str]] = {sab: {} for sab in hier_sabs}

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".partial")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
        conn.executescript(SCHEMA)
        conn.execute("CREATE TEMP TABLE tty_rank (sab TEXT, tty TEXT, rnk INTEGER)")
        conn.executemany(
            "INSERT INTO tty_rank VALUES (?, ?, ?)",
            ((sab, tty, r) for r, sab, tty in _mrrank_rows(mrrank, wanted)),
        )

        def code_rows() -> Iterable[tuple[str, str, str, str, str, str]]:
            rows = _mrconso_rows(mrconso, wanted, include_suppressed)
            for n, row in enumerate(rows, 1):
                sab, aui = row[1], row[3]
                if sab in graph_atoms and aui in graph_atoms[sab]:
                    atom_code[sab][aui] = row[2]  # the atoms the CHD graph mentions
                yield row
                if progress and n % _BATCH == 0:
                    progress("MRCONSO", n)

        _insert_batched(
            conn,
            "INSERT OR IGNORE INTO cui_codes (cui, sab, code, aui, tty, str) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            code_rows(),
        )
        conn.executescript(INDEXES)
        _mark_preferred(conn)

        for sab in hier_sabs:
            n = _insert_batched(
                conn,
                "INSERT INTO code_hierarchy (sab, code, ancestor_code, distance) "
                "VALUES (?, ?, ?, ?)",
                (
                    (sab, code, anc, dist)
                    for code, anc, dist in code_ancestors(
                        relations.chd[sab], atom_code[sab], max_depth=hierarchy_max_depth
                    )
                ),
            )
            if progress:
                progress(f"{sab} hierarchy", n)
        atom_code.clear()  # free the atom maps before the ingredient walk
        graph_atoms.clear()

        if want_rxnorm:
            _build_ingredients(conn, relations.rxnorm, progress)

        conn.execute(
            "INSERT INTO build_info (umls_release, built_at, sources, include_suppressed, "
            "cuiflow_version, hierarchy_sources, hierarchy_max_depth, rxnorm_ingredients) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                release,
                utc_now(),
                ",".join(sorted(wanted)),
                int(include_suppressed),
                __version__,
                ",".join(hier_sabs),
                hierarchy_max_depth if hier_sabs else None,
                int(want_rxnorm),
            ),
        )
        conn.commit()
    except BaseException:
        conn.close()
        tmp.unlink(missing_ok=True)  # never leave a half-built store behind
        raise
    conn.close()
    tmp.replace(out)
    return out


def _mark_preferred(conn: sqlite3.Connection) -> None:
    """Preferred-code rule (DESIGN_PLAN §5.2 rule 1).

    PT rows first; for a (cui, sab) with no PT row, the rows at the best MRRANK term type for
    that source.
    """
    conn.execute("UPDATE cui_codes SET is_preferred = 1 WHERE tty = 'PT'")
    conn.execute(
        """
        WITH ranked AS (
            SELECT c.rowid AS rid, c.cui, c.sab, COALESCE(r.rnk, -1) AS rnk
            FROM cui_codes c LEFT JOIN tty_rank r ON r.sab = c.sab AND r.tty = c.tty
            WHERE NOT EXISTS (
                SELECT 1 FROM cui_codes p
                WHERE p.cui = c.cui AND p.sab = c.sab AND p.is_preferred = 1
            )
        ),
        best AS (SELECT cui, sab, MAX(rnk) AS rnk FROM ranked GROUP BY cui, sab)
        UPDATE cui_codes SET is_preferred = 1
        WHERE rowid IN (
            SELECT ranked.rid FROM ranked
            JOIN best ON best.cui = ranked.cui AND best.sab = ranked.sab
                AND best.rnk = ranked.rnk
        )
        """
    )


def _build_ingredients(
    conn: sqlite3.Connection, graph: dict[str, set[str]], progress: Progress | None
) -> None:
    """Resolve every RxNorm CUI in the store to its ingredients (DESIGN_PLAN §5.2 rule 5)."""
    marks = ",".join("?" * len(INGREDIENT_TTYS))
    # One (rxcui, name) per ingredient CUI: preferred rows first, IN before PIN.
    named: dict[str, tuple[str, str]] = {}
    for cui, code, name in conn.execute(
        f"SELECT cui, code, str FROM cui_codes WHERE sab = 'RXNORM' AND tty IN ({marks}) "
        f"ORDER BY cui, is_preferred DESC, tty, code",
        INGREDIENT_TTYS,
    ):
        named.setdefault(cui, (code, name))
    products = [
        r[0] for r in conn.execute("SELECT DISTINCT cui FROM cui_codes WHERE sab = 'RXNORM'")
    ]
    n = _insert_batched(
        conn,
        "INSERT INTO rxnorm_ingredients "
        "(product_cui, ingredient_cui, ingredient_rxcui, ingredient_name, hops) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            (product, ing, *named[ing], hops)
            for product, ing, hops in resolve_ingredients(graph, named.keys(), products)
        ),
    )
    if progress:
        progress("RxNorm ingredients", n)
