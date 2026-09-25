"""One semantic-group table for both engines (DESIGN_PLAN §3.2).

cTAKES groups (umlsmatch) and NLM's UMLS Semantic Groups are different classifications. Both
engines use umlsmatch's cTAKES table so a group filter means the same thing whichever engine
produced a mention. umlsmatch's base install is standard library only, so importing it here
costs nothing extra.
"""

from __future__ import annotations

from collections.abc import Iterable

UNKNOWN = "UNKNOWN"


def group_for_tuis(tuis: Iterable[str]) -> str:
    """The cTAKES group for a concept's TUIs, by cTAKES' own best-group rule."""
    try:
        from umlsmatch.umls.semantic_tui import best_group, group_for_tui
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError(
            "semantic groups need the umlsmatch package: pip install 'cuiflow[mmlite]'"
        ) from exc
    groups = []
    for tui in tuis:
        try:
            groups.append(group_for_tui(tui))
        except ValueError:
            continue
    name: str = best_group(groups).name
    return name
