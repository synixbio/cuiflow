"""Code-level ancestor walks for ICD-10-CM and SNOMED CT (DESIGN_PLAN §5.2, rules 2-4).

Ported from ``metamap_api/examples/add_icd10cm_hierarchy.py`` and ``add_snomedct_hierarchy.py``,
with their tests (``tests/test_hierarchy.py``). The rules they verified on UMLS 2026AA:

- In MRREL, a ``REL=CHD`` row has the parent in the first column (CUI1/AUI1) and the child in
  the second: ``C0013404(R06.0 Dyspnea)|A17867138|…|CHD|C0085619(R06.01 Orthopnea)|A17803262``.
- Walk over atoms (AUIs), never CUIs: UMLS merges distinct codes into one CUI, which drops
  ancestors (ICD-10-CM ``R06`` and ``R06.9`` share a CUI) or mixes unrelated chains (SNOMED CT's
  lisinopril substance and product codes share a CUI).
- ICD-10-CM and SNOMED CT are DAGs: every ancestor is stored at its shortest distance.

The difference from the scripts: they walked only the codes of concepts matched in a corpus
and backfilled the ancestors' code rows. The store holds every code, so every code is walked
and nothing needs backfilling.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping

#: Sources whose CHD relations the builder turns into ``code_hierarchy`` rows.
HIERARCHY_SOURCES: tuple[str, ...] = ("ICD10CM", "SNOMEDCT_US")


def _ancestor_atoms(
    start: set[str], graph: Mapping[str, set[str]], max_depth: int | None
) -> dict[str, int]:
    """BFS upward from every atom of one code. ``{ancestor_aui: shortest distance}``.

    Never includes the starting atoms themselves, even if the graph loops back to them.
    """
    seen: dict[str, int] = {}
    frontier = list(start)
    hop = 0
    while frontier and (max_depth is None or hop < max_depth):
        hop += 1
        nxt = []
        for aui in frontier:
            for parent in graph.get(aui, ()):
                if parent not in seen and parent not in start:
                    seen[parent] = hop
                    nxt.append(parent)
        frontier = nxt
    return seen


def code_ancestors(
    graph: Mapping[str, set[str]],
    atom_code: Mapping[str, str],
    *,
    max_depth: int | None = None,
) -> Iterator[tuple[str, str, int]]:
    """``(code, ancestor_code, distance)`` for every code of one source, codes in sorted order.

    ``graph`` maps a child AUI to its parent AUIs (from CHD rows). ``atom_code`` maps each AUI
    the store kept to its code; an ancestor atom missing from it (suppressed, say) is walked
    through but not reported. A code's own other atoms are never its ancestors.
    """
    if max_depth is not None and max_depth < 1:
        raise ValueError(f"max_depth must be at least 1 (or None for no limit), got {max_depth}")
    atoms_of: dict[str, set[str]] = defaultdict(set)
    for aui, code in atom_code.items():
        atoms_of[code].add(aui)
    for code in sorted(atoms_of):
        best: dict[str, int] = {}
        for aui, dist in _ancestor_atoms(atoms_of[code], graph, max_depth).items():
            anc = atom_code.get(aui)
            if anc is not None and anc != code and dist < best.get(anc, dist + 1):
                best[anc] = dist
        for anc in sorted(best, key=lambda c: (best[c], c)):
            yield code, anc, best[anc]
