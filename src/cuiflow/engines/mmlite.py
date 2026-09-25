"""Adapter for mmlite, the Python port of NLM MetaMapLite.

Mapping to the canonical schema (DESIGN_PLAN §3.2):

- An mmlite ``Entity`` carries several candidate concepts (``evs``); each distinct CUI becomes
  its own ``Mention`` at that span.
- ``semantic_types`` are MetaMapLite abbreviations ("dsyn"); they are converted to TUIs, then to
  a cTAKES group with umlsmatch's table.
- ``subject`` and ``temporality`` are set only when ConText ran. Under NegEx they stay ``None``:
  nothing assessed them.

The only mmlite settings cuiflow exposes are the index (``mmlite_index``) and the negation
detector (``mmlite_negation``). The rest are mmlite's defaults, and subsumed candidates are
always removed; cuiflow's ``profile`` changes umlsmatch only. ``info()`` records both settings.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from cuiflow.core.models import AssertionStatus, Mention, ProvenanceInfo, make_mention_id
from cuiflow.engines.base import EngineInfo
from cuiflow.engines.groups import group_for_tuis

# mmlite's ConText emits only "Patient" or "Other": its family triggers ("family history of",
# "mother", ...) yield "Other", so mmlite never reports ``family_member`` and cuiflow does not
# guess one from the trigger. The family spellings are mapped in case a later mmlite adds them.
_EXPERIENCER = {
    "patient": "patient",
    "other": "other",
    "family": "family_member",
    "family_member": "family_member",
}
_TEMPORALITY = {"recent": "recent", "historical": "historical", "hypothetical": "hypothetical"}


class MmliteEngine:
    name = "mmlite"

    def __init__(
        self,
        index_directory: Path | str | None = None,
        *,
        negation: str = "negex",
        keep_subsumed: bool = False,
    ) -> None:
        try:
            import mmlite
            from mmlite import MetaMapLite
            from mmlite.config import Settings
            from mmlite.semtypes import to_tui
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ImportError(
                "the mmlite engine needs the 'mmlite' extra: pip install 'cuiflow[mmlite]'"
            ) from exc

        overrides: dict[str, Any] = {}
        if index_directory is not None:
            overrides["index_directory"] = Path(index_directory)
        settings = Settings.load(**overrides)
        settings = dataclasses.replace(settings, negation_detector=negation)
        self._to_tui = to_tui
        self._version: str = mmlite.__version__
        self._negation = negation
        self._keep_subsumed = keep_subsumed
        self._mml = MetaMapLite(settings, remove_subsumed=not keep_subsumed)
        self._index_dir = settings.index_directory

    def info(self) -> EngineInfo:
        return EngineInfo(
            name=self.name,
            version=self._version,
            dictionary=str(self._index_dir),
            settings={"negation": self._negation, "keep_subsumed": self._keep_subsumed},
        )

    def _tuis(self, abbrevs: frozenset[str]) -> tuple[str, ...]:
        out = set()
        for a in abbrevs:
            try:
                out.add(self._to_tui(a))
            except (KeyError, ValueError):
                continue
        return tuple(sorted(out))

    def _assertions(self, entity: Any) -> AssertionStatus:
        if self._negation != "context":
            return AssertionStatus(negated=bool(entity.negated))
        experiencer = (entity.experiencer or "").lower()
        temporality = (entity.temporality or "").lower()
        return AssertionStatus(
            negated=bool(entity.negated),
            subject=_EXPERIENCER.get(experiencer),  # type: ignore[arg-type]
            temporality=_TEMPORALITY.get(temporality),  # type: ignore[arg-type]
        )

    def extract(self, text: str, doc_id: str) -> list[Mention]:
        entities = self._mml.process_text(text, fieldid=None)
        mentions: list[Mention] = []
        for e in entities:
            start, end = e.start, e.start + e.length
            assertions = self._assertions(e)
            seen: set[str] = set()
            for ev in e.evs:
                concept = ev.concept
                if concept.cui in seen:
                    continue
                seen.add(concept.cui)
                tuis = self._tuis(concept.semantic_types)
                mentions.append(
                    Mention(
                        mention_id=make_mention_id(doc_id, start, end, concept.cui),
                        start=start,
                        end=end,
                        text=text[start:end],
                        cui=concept.cui,
                        preferred_name=concept.preferred_name,
                        semantic_group=group_for_tuis(tuis),
                        assertions=assertions,
                        provenance=ProvenanceInfo(
                            found_by=(self.name,),
                            semantic_types=tuis,
                            matched_term=concept.concept_string,
                        ),
                    )
                )
        return mentions

    def lookup_term(self, term: str) -> list[tuple[str, str]]:
        """``(cui, preferred_name)`` for a bare term: MetaMapLite's normalized dictionary lookup."""
        out: dict[str, str] = {}
        for e in self._mml.lookup_term(term):
            for ev in e.evs:
                out.setdefault(ev.concept.cui, ev.concept.preferred_name)
        return list(out.items())

    def close(self) -> None:
        self._mml.close()
