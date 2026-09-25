"""Combining the two engines (DESIGN_PLAN §4.2-4.3).

Only ``consensus`` passes its gate, and only on the synthetic gold set (DESIGN_PLAN §11,
Phase 3; results in docs/EVALUATION.md). No ensemble is a default: none has been measured on
real notes.

- ``union``: mentions found by either engine. Two mentions merge when they share a CUI and
  their spans are equal after trimming edge whitespace and punctuation.
- ``consensus``: only mentions both engines found with the same CUI on overlapping spans,
  paired one-to-one with as many pairs as possible (the evaluation's overlap matching).
- ``hybrid_staged``: concepts from mmlite, assertions from umlsmatch's rules via its
  ``ClinicalPipeline.assess`` (§4.1), added in umlsmatch 0.2.0.

When engines disagree on an assertion, both answers are kept in
``provenance.engine_assertions`` and ``assertion_conflict`` is set; the combined value follows
the configured :class:`~cuiflow.core.enums.ConflictPolicy`.
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from cuiflow.core.alignment import match_concepts, trimmed_span
from cuiflow.core.enums import ConflictPolicy, EngineMode
from cuiflow.core.models import (
    ASSERTION_ATTRIBUTES,
    AssertionStatus,
    Mention,
    ProvenanceInfo,
    make_mention_id,
)
from cuiflow.engines.base import Engine, EngineInfo


def priority_order(policy: ConflictPolicy) -> tuple[str, str]:
    """Which engine's fields win when merging (and whose assertions, under a prefer: policy)."""
    if policy is ConflictPolicy.PREFER_MMLITE:
        return ("mmlite", "umlsmatch")
    return ("umlsmatch", "mmlite")


def merge_assertions(
    per_engine: Mapping[str, AssertionStatus], policy: ConflictPolicy
) -> tuple[AssertionStatus, bool, dict[str, dict[str, Any]]]:
    """Combine engines' assertions for one mention.

    Returns ``(combined, conflict, engine_assertions)``. An attribute assessed by only one
    engine is taken from it without conflict; ``engine_assertions`` is filled only on conflict.
    """
    order = priority_order(policy)
    combined: dict[str, Any] = {}
    conflict = False
    for attr in ASSERTION_ATTRIBUTES:
        values = {
            eng: getattr(a, attr) for eng, a in per_engine.items() if getattr(a, attr) is not None
        }
        distinct = set(values.values())
        if len(distinct) <= 1:
            combined[attr] = next(iter(distinct), None)
            continue
        conflict = True
        if policy is ConflictPolicy.UNKNOWN:
            combined[attr] = None
        elif policy is ConflictPolicy.NEGATED_IF_ANY and all(isinstance(v, bool) for v in distinct):
            combined[attr] = True in distinct
        else:
            combined[attr] = next(values[e] for e in order if e in values)
    engine_assertions = {eng: a.to_dict() for eng, a in per_engine.items()} if conflict else {}
    return AssertionStatus(**combined), conflict, engine_assertions


def _merge(group: Sequence[Mention], doc_id: str, policy: ConflictPolicy) -> Mention:
    """One mention from same-concept mentions of different engines."""
    order = priority_order(policy)
    by_engine = {m.provenance.found_by[0]: m for m in reversed(group)}
    base = next(by_engine[e] for e in order if e in by_engine)
    if len(by_engine) == 1:
        return base
    assertions, conflict, engine_assertions = merge_assertions(
        {e: m.assertions for e, m in by_engine.items()}, policy
    )
    tuis = sorted({t for m in group for t in m.provenance.semantic_types})
    return Mention(
        mention_id=make_mention_id(doc_id, base.start, base.end, base.cui),
        start=base.start,
        end=base.end,
        text=base.text,
        cui=base.cui,
        preferred_name=base.preferred_name,
        semantic_group=base.semantic_group,
        assertions=assertions,
        section=base.section or next((m.section for m in group if m.section), None),
        provenance=ProvenanceInfo(
            found_by=tuple(e for e in sorted(by_engine)),
            semantic_types=tuple(tuis),
            matched_term=base.provenance.matched_term,
            engine_assertions=engine_assertions,
            assertion_conflict=conflict,
        ),
    )


def union(
    text: str,
    by_engine: Mapping[str, Sequence[Mention]],
    doc_id: str,
    policy: ConflictPolicy,
) -> list[Mention]:
    groups: dict[tuple[int, int, str], list[Mention]] = {}
    for mentions in by_engine.values():
        for m in mentions:
            key = (*trimmed_span(text, m.start, m.end), m.cui)
            groups.setdefault(key, []).append(m)
    merged = [_merge(g, doc_id, policy) for g in groups.values()]
    return sorted(merged, key=lambda m: (m.start, m.end, m.cui))


def consensus(
    text: str,
    by_engine: Mapping[str, Sequence[Mention]],
    doc_id: str,
    policy: ConflictPolicy,
) -> list[Mention]:
    first, second = priority_order(policy)
    # A maximum one-to-one matching, as the evaluation scores: a greedy first-come pairing
    # could spend a mention on one partner and leave a second, agreeing pair unmatched.
    pairs = match_concepts(text, by_engine.get(first, ()), by_engine.get(second, ()))
    out = [_merge([m, o], doc_id, policy) for m, o in pairs]
    return sorted(out, key=lambda m: (m.start, m.end, m.cui))


class EnsembleEngine:
    """Runs both engines on each document and combines their mentions."""

    def __init__(
        self,
        engines: Mapping[str, Engine],
        mode: EngineMode,
        policy: ConflictPolicy = ConflictPolicy.PREFER_UMLSMATCH,
    ) -> None:
        if mode is EngineMode.HYBRID:
            raise ValueError("ensemble:hybrid_staged is built by HybridEngine, not EnsembleEngine")
        if not mode.is_ensemble:
            raise ValueError(f"{mode} is not an ensemble mode")
        missing = set(mode.engines) - set(engines)
        if missing:
            raise ValueError(f"{mode} needs engine(s): {', '.join(sorted(missing))}")
        self.name = mode.value
        self._engines = dict(engines)
        self._mode = mode
        self._policy = policy

    def info(self) -> EngineInfo:
        return EngineInfo(
            name=self.name,
            version="",
            settings={
                "conflict_policy": self._policy.value,
                "engines": {n: e.info().to_dict() for n, e in self._engines.items()},
            },
        )

    def extract(self, text: str, doc_id: str) -> list[Mention]:
        by_engine = {name: e.extract(text, doc_id) for name, e in self._engines.items()}
        combine = union if self._mode is EngineMode.UNION else consensus
        return combine(text, by_engine, doc_id, self._policy)

    def close(self) -> None:
        # Every engine is closed even if one fails; the first failure is raised after.
        with contextlib.ExitStack() as stack:
            for e in self._engines.values():
                stack.callback(e.close)


class Assessor(Protocol):
    """An engine that can run its assertion rules over another engine's mentions."""

    name: str

    @property
    def supports_assess(self) -> bool: ...

    def info(self) -> EngineInfo: ...

    def assess(self, text: str, mentions: Sequence[Mention]) -> list[AssertionStatus | None]: ...

    def close(self) -> None: ...


class HybridEngine:
    """``ensemble:hybrid_staged``: one engine's concepts, another's assertion rules.

    Concepts (spans, CUIs, groups) come from ``concepts`` (mmlite); every assertion attribute
    comes from ``assessor`` (umlsmatch). A mention the assessor cannot assess (it crosses one
    of the assessor's sentence boundaries) gets all attributes ``None``: not assessed, never
    the concept engine's answer passed off as the assessor's (DESIGN_PLAN §4.1). The concept
    engine's own assertions are kept in ``provenance.engine_assertions`` for comparison.
    """

    name = EngineMode.HYBRID.value

    def __init__(self, concepts: Engine, assessor: Assessor) -> None:
        if not getattr(assessor, "supports_assess", False):
            raise NotImplementedError(
                "ensemble:hybrid_staged needs umlsmatch's ClinicalPipeline.assess(); upgrade "
                "umlsmatch to 0.2.0 or later (DESIGN_PLAN section 4.1)"
            )
        self._concepts = concepts
        self._assessor = assessor

    def info(self) -> EngineInfo:
        return EngineInfo(
            name=self.name,
            version="",
            settings={
                "concepts": self._concepts.info().to_dict(),
                "assertions": self._assessor.info().to_dict(),
            },
        )

    def extract(self, text: str, doc_id: str) -> list[Mention]:
        mentions = self._concepts.extract(text, doc_id)
        if not mentions:
            return []
        assessed = self._assessor.assess(text, mentions)
        out = []
        for m, a in zip(mentions, assessed, strict=True):
            provenance = dataclasses.replace(
                m.provenance,
                engine_assertions={self._concepts.name: m.assertions.to_dict()},
                assessed_by=self._assessor.name,
            )
            out.append(
                dataclasses.replace(
                    m, assertions=a if a is not None else AssertionStatus(), provenance=provenance
                )
            )
        return out

    def close(self) -> None:
        with contextlib.ExitStack() as stack:
            stack.callback(self._assessor.close)
            stack.callback(self._concepts.close)  # runs first, as before
