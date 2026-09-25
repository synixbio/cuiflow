"""Adapter for umlsmatch, the cTAKES-style clinical pipeline.

umlsmatch already follows the ``None``-means-not-assessed rule, so its attributes pass through
unchanged. Its annotations do not carry TUIs, so ``provenance.semantic_types`` stays empty.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, get_args

from cuiflow.core.models import (
    AssertionStatus,
    Mention,
    ProvenanceInfo,
    Subject,
    make_mention_id,
)
from cuiflow.engines.base import EngineInfo

_SUBJECTS: frozenset[str] = frozenset(get_args(Subject))


def _subject(value: str | None) -> Subject | None:
    """umlsmatch types ``subject`` as plain ``str``; refuse anything outside the schema."""
    if value is None:
        return None
    if value not in _SUBJECTS:
        raise ValueError(f"umlsmatch returned an unknown subject {value!r}")
    return value  # type: ignore[return-value]  # narrowed by the membership check


def _assertions(a: Any) -> AssertionStatus:
    """An umlsmatch Annotation's attributes; umlsmatch already uses None for "not assessed"."""
    return AssertionStatus(
        negated=a.negated,
        subject=_subject(a.subject),
        history_of=a.history_of,
        uncertain=a.uncertain,
        conditional=a.conditional,
        generic=a.generic,
    )


class UmlsmatchEngine:
    name = "umlsmatch"

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        profile: str = "strict",
        resolve_overlaps: bool = False,
        conditional: bool = False,
    ) -> None:
        try:
            import umlsmatch
            from umlsmatch.analyze import ClinicalPipeline
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ImportError(
                "the umlsmatch engine needs the 'umlsmatch' extra: pip install 'cuiflow[umlsmatch]'"
            ) from exc
        self._version: str = umlsmatch.__version__
        self._profile = profile
        self._pipeline = ClinicalPipeline(
            db_path,
            profile=profile,
            resolve_overlaps=resolve_overlaps,
            conditional=conditional,
        )

    def info(self) -> EngineInfo:
        return EngineInfo(
            name=self.name,
            version=self._version,
            dictionary=str(self._pipeline.db_path),
            settings={
                "profile": self._profile,
                "assessed_attributes": sorted(self._pipeline.assessed_attributes),
            },
        )

    def extract(self, text: str, doc_id: str) -> list[Mention]:
        mentions = []
        for a in self._pipeline.analyze(text):
            mentions.append(
                Mention(
                    mention_id=make_mention_id(doc_id, a.start, a.end, a.cui),
                    start=a.start,
                    end=a.end,
                    text=a.text,
                    cui=a.cui,
                    preferred_name=a.preferred_text,
                    semantic_group=a.group,
                    assertions=_assertions(a),
                    provenance=ProvenanceInfo(found_by=(self.name,), matched_term=a.term),
                )
            )
        return mentions

    @property
    def supports_assess(self) -> bool:
        """Whether the installed umlsmatch has ``ClinicalPipeline.assess`` (DESIGN_PLAN §4.1)."""
        return callable(getattr(self._pipeline, "assess", None))

    def assess(self, text: str, mentions: Sequence[Mention]) -> list[AssertionStatus | None]:
        """umlsmatch's assertion rules over another engine's mentions, in input order.

        ``None`` where umlsmatch could not assess a span (it crosses a sentence boundary or
        covers no token).
        """
        # Looked up at run time: releases before 0.2.0 lack it (DESIGN_PLAN §4.1).
        assess: Any = getattr(self._pipeline, "assess", None)
        if not callable(assess):
            raise NotImplementedError(
                "this umlsmatch has no ClinicalPipeline.assess(); upgrade it to 0.2.0 or later"
            )
        spans = [(m.start, m.end, m.cui) for m in mentions]
        return [None if a is None else _assertions(a) for a in assess(text, spans)]

    def close(self) -> None:
        self._pipeline.close()
