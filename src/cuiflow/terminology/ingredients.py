"""RxNorm product → ingredient resolution (DESIGN_PLAN §5.2, rule 5).

Ported from ``metamap_api/examples/add_rxnorm_ingredients.py``, with its tests
(``tests/test_hierarchy.py``). The rules it verified on UMLS 2026AA:

- MRREL ``CUI1|…|RELA|CUI2`` reads "CUI2 RELA CUI1":
  ``C0065374(lisinopril, IN)|RO|C0987217(lisinopril 10 MG, SCDC)|has_ingredient`` means the
  component has ingredient lisinopril.
- Walk ``SBD --tradename_of--> SCD --consists_of--> SCDC --has_ingredient--> IN``, to any depth.
- Accept only endpoints with TTY ``IN`` or ``PIN``; some ``has_ingredient`` edges end at a
  brand name (``BN``). An unconfirmed endpoint is a dead end, not a result.
- Use ``tradename_of`` only, never ``has_tradename``: MRREL stores the two with opposite column
  orders for the same kind of pair, so including both adds wrong-way edges.

One deliberate difference: the script counted a CUI as an ingredient even when its only IN/PIN
atoms were suppressed. Here the ingredient set comes from the store's rows, so a retired
ingredient (suppressed unless the store keeps suppressed rows) is not reported.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Mapping

#: The MRREL RELA values the walk follows (see the module docstring for why not has_tradename).
BREAKDOWN_RELAS: frozenset[str] = frozenset({"has_ingredient", "consists_of", "tradename_of"})
INGREDIENT_TTYS: tuple[str, ...] = ("IN", "PIN")


def _resolve(
    cui: str, graph: Mapping[str, set[str]], ingredients: Collection[str]
) -> dict[str, int]:
    """``{ingredient_cui: hops}`` for every confirmed ingredient reachable from ``cui``.

    A reachable node counts only if it has no further breakdown edge and is a confirmed
    ingredient. ``{cui: 0}`` when ``cui`` is itself an ingredient.
    """
    seen = {cui: 0}
    frontier = [cui]
    hop = 0
    while frontier:
        hop += 1
        nxt = []
        for c in frontier:
            for finer in graph.get(c, ()):
                if finer not in seen:
                    seen[finer] = hop
                    nxt.append(finer)
        frontier = nxt
    return {c: h for c, h in seen.items() if c not in graph and c in ingredients}


def resolve_ingredients(
    graph: Mapping[str, set[str]],
    ingredients: Collection[str],
    products: Iterable[str],
) -> Iterator[tuple[str, str, int]]:
    """``(product_cui, ingredient_cui, hops)`` for each product that resolves.

    ``graph`` maps a coarser CUI to finer ones (``graph[CUI2].add(CUI1)`` for each breakdown
    row). Products with no path to a confirmed ingredient yield nothing.
    """
    for product in sorted(set(products)):
        found = _resolve(product, graph, ingredients)
        for ingredient in sorted(found):
            yield product, ingredient, found[ingredient]
