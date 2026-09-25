"""Run engine modes over a reference set and report the scores (DESIGN_PLAN §11, Phase 1).

Each underlying engine is built once and shared by every mode that needs it, so comparing four
modes loads each index once. Scores are restricted to a stated set of semantic groups on *both*
sides, because the engines index different scopes (umlsmatch's dictionary covers cTAKES' 49
TUIs; mmlite covers every semantic type) and scoring outside a shared scope measures scope, not
extraction.

Reports are meant to be shared, so they name each index by its file name and the UMLS release it
records, never by its local path.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cuiflow.api import Extractor
from cuiflow.core.config import ExtractionConfig
from cuiflow.core.enums import EngineMode
from cuiflow.engines import Engine, create_base_engine
from cuiflow.engines.base import EngineInfo
from cuiflow.engines.ensemble import EnsembleEngine, HybridEngine
from cuiflow.evaluation.metrics import Score
from cuiflow.evaluation.reference import ReferenceMention, ReferenceSet
from cuiflow.evaluation.scoring import POSITIVE, ModeScores, score_document

#: cTAKES' five "major" groups: the scope most deployments consume, and the default here.
MAJOR_GROUPS: tuple[str, ...] = ("DISORDER", "FINDING", "DRUG", "PROCEDURE", "ANATOMY")


@dataclass
class EvaluationReport:
    reference: ReferenceSet
    groups: tuple[str, ...]
    config: dict[str, Any]
    engines: dict[str, dict[str, Any]]
    modes: dict[str, ModeScores] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    def to_dict(self, *, per_document: bool = False) -> dict[str, Any]:
        from cuiflow import __version__

        def prf(s: Score) -> dict[str, Any]:
            return {
                "precision": round(s.precision, 4),
                "recall": round(s.recall, 4),
                "f1": round(s.f1, 4),
                "tp": s.tp,
                "fp": s.fp,
                "fn": s.fn,
            }

        modes: dict[str, Any] = {}
        for name, m in self.modes.items():
            entry: dict[str, Any] = {
                "reference_mentions": m.reference_mentions,
                "predicted_mentions": m.predicted_mentions,
                "overlap": prf(m.overlap),
                "exact": prf(m.exact),
                "cui_set": prf(m.cui_set),
                "attributes": {
                    a: {
                        "reference_assessed": s.reference_assessed,
                        "coverage": round(s.coverage, 4),
                        **prf(s.score),
                    }
                    for a, s in sorted(m.attributes.items())
                },
                "seconds": self.seconds.get(name),
            }
            if per_document:
                entry["per_document"] = {
                    d: {"overlap": prf(s.overlap), "cui_set": prf(s.cui_set)}
                    for d, s in m.per_document.items()
                }
            modes[name] = entry
        return {
            "cuiflow_version": __version__,
            "created_at": self.created_at,
            "reference": {
                "name": self.reference.name,
                "kind": self.reference.kind,
                "documents": len(self.reference.docs),
                "caveat": self.reference.caveat,
            },
            "scope_groups": list(self.groups) or "all",
            "config": self.config,
            "engines": self.engines,
            "modes": modes,
        }

    def to_markdown(self) -> str:
        d = self.to_dict()
        ref = d["reference"]
        scope = ", ".join(self.groups) if self.groups else "all groups"
        lines = [
            f"# Evaluation: {ref['name']} ({ref['kind']})",
            "",
            f"- Documents: {ref['documents']}; scope: {scope}; cuiflow {d['cuiflow_version']}; "
            f"{d['created_at']}",
            f"- **Caveat:** {ref['caveat']}",
            "",
            "## Concepts",
            "",
            "| mode | ref | pred | overlap P / R / F1 | exact F1 | CUI-set F1 |",
            "|---|---:|---:|---|---:|---:|",
        ]
        for name, m in d["modes"].items():
            o = m["overlap"]
            lines.append(
                f"| `{name}` | {m['reference_mentions']} | {m['predicted_mentions']} | "
                f"{o['precision']:.3f} / {o['recall']:.3f} / **{o['f1']:.3f}** | "
                f"{m['exact']['f1']:.3f} | {m['cui_set']['f1']:.3f} |"
            )
        attrs = sorted({a for m in d["modes"].values() for a in m["attributes"]})
        if attrs:
            lines += [
                "",
                "## Assertions (agreement with the reference's labels, on overlap-matched pairs)",
                "",
                "| mode | attribute | ref labels | positives | coverage | P / R / F1 |",
                "|---|---|---:|---:|---:|---|",
            ]
            for name, m in d["modes"].items():
                for a in attrs:
                    s = m["attributes"].get(a)
                    if s is None:
                        continue
                    positives = s["tp"] + s["fn"]
                    lines.append(
                        f"| `{name}` | {a} | {s['reference_assessed']} | {positives} | "
                        f"{s['coverage']:.3f} | {s['precision']:.3f} / {s['recall']:.3f} / "
                        f"{s['f1']:.3f} |"
                    )
        lines += ["", "## Engines", ""]
        for name, info in d["engines"].items():
            release = f", UMLS {info['umls_release']}" if info.get("umls_release") else ""
            lines.append(f"- `{name}` {info.get('version', '')}: {info.get('dictionary')}{release}")
        return "\n".join(lines) + "\n"


def _file_name(path: object) -> str | None:
    return None if path is None else Path(str(path)).name


def _portable_config(cfg: ExtractionConfig) -> dict[str, Any]:
    """The effective configuration, with index paths reduced to their file names."""
    out = cfg.to_dict()
    for name in ("mmlite_index", "umlsmatch_db", "terminology_db"):
        out[name] = _file_name(out[name])
    return out


def _portable_engine(info: EngineInfo) -> dict[str, Any]:
    out = info.to_dict()
    out["dictionary"] = _file_name(out["dictionary"])
    return out


def _reference_in_scope(
    mentions: Sequence[ReferenceMention], groups: frozenset[str]
) -> list[ReferenceMention]:
    if not groups:
        return list(mentions)
    from cuiflow.engines.groups import group_for_tuis

    return [m for m in mentions if (m.group or group_for_tuis(m.tuis)).upper() in groups]


def run_evaluation(
    reference: ReferenceSet,
    modes: Sequence[EngineMode],
    config: ExtractionConfig,
    *,
    groups: Sequence[str] = MAJOR_GROUPS,
    engines: Mapping[str, Engine] | None = None,
) -> EvaluationReport:
    """Score each mode on every reference document.

    ``engines`` injects prebuilt base engines (tests); otherwise each needed engine is built
    from ``config`` once and closed at the end.
    """
    scope = frozenset(g.upper() for g in groups)
    # Scoring does its own group filter; codes are irrelevant to extraction scores.
    cfg = dataclasses.replace(config, semantic_groups=(), code_systems=(), include_text=False)
    needed = sorted({name for mode in modes for name in mode.engines})
    owned: dict[str, Engine] = {}
    base: dict[str, Engine] = dict(engines or {})
    try:
        for name in needed:
            if name not in base:
                owned[name] = base[name] = create_base_engine(name, cfg)
        report = EvaluationReport(
            reference=reference,
            groups=tuple(sorted(scope)),
            config=_portable_config(cfg),
            engines={n: _portable_engine(base[n].info()) for n in needed},
        )
        attributes = sorted(reference.assessed_attributes & POSITIVE.keys())
        for mode in modes:
            engine: Engine
            if mode is EngineMode.HYBRID:
                engine = HybridEngine(base["mmlite"], base["umlsmatch"])  # type: ignore[arg-type]
            elif mode.is_ensemble:
                engine = EnsembleEngine(
                    {n: base[n] for n in mode.engines}, mode, cfg.conflict_policy
                )
            else:
                engine = base[mode.engines[0]]
            extractor = Extractor(dataclasses.replace(cfg, mode=mode), engine=engine)
            scores = ModeScores()
            started = time.perf_counter()
            for doc in reference.docs:
                result = extractor.process(doc.text, doc.doc_id)
                predicted = [
                    m for m in result.mentions if not scope or m.semantic_group.upper() in scope
                ]
                score_document(
                    doc, _reference_in_scope(doc.mentions, scope), predicted, attributes, scores
                )
            report.seconds[mode.value] = round(time.perf_counter() - started, 2)
            report.modes[mode.value] = scores
            extractor.close()  # leaves the injected, shared base engines open
        return report
    finally:
        for e in owned.values():
            e.close()
