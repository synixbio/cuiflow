"""Reference annotation sets, loaded into one shape (DESIGN_PLAN §11, Phase 1).

Three formats are read:

- ``cuiflow``: cuiflow's own reference format (below), for a human-annotated gold set.
- ``ctakes-silver``: umlsmatch's ``silver.jsonl`` (real Java cTAKES output, via
  ``tools/run_java_ctakes.py``), with cTAKES' assertion labels.
- ``mmlite-fixtures``: Java MetaMapLite's ``--outputformat json`` files from mmlite's parity
  suite, plus the corpus they were produced from. Concepts only: MetaMapLite's JSON carries no
  assertion labels, so those stay ``None`` (not assessed).

**A tool's output is a reference, not a gold standard.** Scores against cTAKES or MetaMapLite
measure agreement with that tool, and favour the engine that reproduces it (umlsmatch and mmlite
respectively). Only the ``cuiflow`` format, filled by human annotators, measures accuracy.

cuiflow reference format: one JSON object per line::

    {"doc_id": "doc_01", "text": "...", "source": "human:annotator-a",
     "mentions": [{"start": 10, "end": 20, "text": "chest pain", "cui": "C0008031",
                   "group": "FINDING", "assertions": {"negated": true, ...}}]}

``group`` (a cTAKES group name) or ``tuis`` places a mention in an evaluation scope.
:mod:`cuiflow.evaluation.gold` builds this format from an annotation worksheet, and its
``validate_records`` defines what a valid file is.

Reference files built from UMLS content (CUIs, names) are licensed data: keep them out of git.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cuiflow.core.models import ASSERTION_ATTRIBUTES, AssertionStatus

FORMATS = ("cuiflow", "ctakes-silver", "mmlite-fixtures")


@dataclass(frozen=True, slots=True)
class ReferenceMention:
    start: int
    end: int
    cui: str
    tuis: tuple[str, ...] = ()
    assertions: AssertionStatus = field(default_factory=AssertionStatus)
    #: Semantic group chosen by a human annotator. Tool references leave it None and the group
    #: is derived from ``tuis``.
    group: str | None = None


@dataclass(frozen=True, slots=True)
class ReferenceDoc:
    doc_id: str
    text: str
    mentions: tuple[ReferenceMention, ...]


@dataclass(frozen=True, slots=True)
class ReferenceSet:
    name: str
    kind: str  # one of FORMATS
    caveat: str
    docs: tuple[ReferenceDoc, ...]

    @property
    def assessed_attributes(self) -> frozenset[str]:
        return frozenset(a for d in self.docs for m in d.mentions for a in m.assertions.assessed())


def _assertions(d: Mapping[str, Any]) -> AssertionStatus:
    return AssertionStatus(**{k: d.get(k) for k in ASSERTION_ATTRIBUTES})


def load_cuiflow(path: Path) -> ReferenceSet:
    docs = []
    sources: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            sources.add(rec.get("source", "unknown"))
            mentions = tuple(
                ReferenceMention(
                    start=m["start"],
                    end=m["end"],
                    cui=m["cui"],
                    tuis=tuple(m.get("tuis", ())),
                    assertions=_assertions(m.get("assertions", {})),
                    group=m.get("group"),
                )
                for m in rec["mentions"]
            )
            docs.append(ReferenceDoc(rec["doc_id"], rec["text"], mentions))
    if sources and all(s.startswith("human:") for s in sources):
        caveat = "Human-annotated reference: scores are accuracy against this set."
    else:
        caveat = (
            f"Reference sources: {', '.join(sorted(sources))}. Not human-annotated: "
            "scores are agreement with this reference, not accuracy."
        )
    return ReferenceSet(name=path.name, kind="cuiflow", caveat=caveat, docs=tuple(docs))


def load_ctakes_silver(path: Path) -> ReferenceSet:
    """umlsmatch's silver.jsonl: one record per note with cTAKES mentions and labels."""
    docs = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            mentions = []
            for m in rec["mentions"]:
                labels = AssertionStatus(
                    negated=m.get("negated"),
                    subject=m.get("subject"),
                    history_of=m.get("history_of"),
                    uncertain=m.get("uncertain"),
                    conditional=m.get("conditional"),
                    generic=m.get("generic"),
                )
                # One reference mention per (span, CUI); a CUI can recur with several TUIs.
                tuis_by_cui: dict[str, set[str]] = {}
                for c in m["concepts"]:
                    tuis_by_cui.setdefault(c["cui"], set()).update(
                        [c["tui"]] if c.get("tui") else []
                    )
                for cui, tuis in tuis_by_cui.items():
                    mentions.append(
                        ReferenceMention(m["begin"], m["end"], cui, tuple(sorted(tuis)), labels)
                    )
            docs.append(ReferenceDoc(rec["source_file"], rec["text"], tuple(mentions)))
    return ReferenceSet(
        name=path.name,
        kind="ctakes-silver",
        caveat=(
            "Java cTAKES output, not a gold standard: scores are agreement with cTAKES, which "
            "favours umlsmatch (a cTAKES reimplementation). cTAKES used its shipped 2016AB "
            "dictionary; the engines here may use another release. cTAKES' assertion labels are "
            "measurably wrong on some constructions (see umlsmatch docs/ADJUDICATION_RESULTS.md)."
        ),
        docs=tuple(docs),
    )


def load_mmlite_fixtures(fixtures: Path, corpus: Path) -> ReferenceSet:
    """Java MetaMapLite JSON output (``<name>.json``) paired with ``corpus/<name>.txt``."""
    try:
        from mmlite.semtypes import to_tui
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError("mmlite-fixtures needs the mmlite package") from exc

    def tuis_of(abbrevs: Iterable[str]) -> tuple[str, ...]:
        out = set()
        for a in abbrevs:
            try:
                out.add(to_tui(a))
            except (KeyError, ValueError):
                continue
        return tuple(sorted(out))

    docs = []
    for txt in sorted(corpus.glob("*.txt")):
        js = fixtures / (txt.stem + ".json")
        if not js.is_file():
            continue
        with txt.open(encoding="utf-8", newline="") as fh:
            text = fh.read()
        mentions: dict[tuple[int, int, str], ReferenceMention] = {}
        for entity in json.loads(js.read_text(encoding="utf-8")):
            start, end = entity["start"], entity["start"] + entity["length"]
            for ev in entity.get("evlist", ()):
                info = ev["conceptinfo"]
                key = (start, end, info["cui"])
                mentions.setdefault(
                    key, ReferenceMention(start, end, info["cui"], tuis_of(info["semantictypes"]))
                )
        docs.append(ReferenceDoc(txt.name, text, tuple(mentions.values())))
    if not docs:
        raise FileNotFoundError(f"no <name>.json in {fixtures} matches a .txt in {corpus}")
    return ReferenceSet(
        name=fixtures.name,
        kind="mmlite-fixtures",
        caveat=(
            "Java MetaMapLite 3.6.2rc8 output, not a gold standard: scores are agreement with "
            "MetaMapLite, which favours mmlite (a MetaMapLite port). Concepts only: MetaMapLite's "
            "JSON carries no assertion labels."
        ),
        docs=tuple(docs),
    )


def load_reference(path: Path, fmt: str, *, corpus: Path | None = None) -> ReferenceSet:
    if fmt == "cuiflow":
        return load_cuiflow(path)
    if fmt == "ctakes-silver":
        return load_ctakes_silver(path)
    if fmt == "mmlite-fixtures":
        if corpus is None:
            raise ValueError("mmlite-fixtures needs --corpus (the .txt files it was run on)")
        return load_mmlite_fixtures(path, corpus)
    raise ValueError(f"unknown reference format {fmt!r}; choose one of {', '.join(FORMATS)}")
