#!/usr/bin/env python
"""Summarize what each note asserts: problems, findings, medications and procedures, each split
into affirmed, negated, someone else's and hedged.

Usage:
    python examples/note_summary.py synthetic/doc_001.txt
    python examples/note_summary.py synthetic/ --groups DISORDER DRUG --limit 5
    python examples/note_summary.py out/runs/<run-id> --json > summaries.jsonl
    python examples/note_summary.py synthetic/ --codes SNOMEDCT_US,RXNORM --ingredients

A note's mentions become one line per **concept** (CUI), not per mention, so "HTN" and
"hypertension" in the same note collapse to one entry. A concept is listed as affirmed when any
mention of it is: not negated, about the patient and not hedged, the same rule as
``notes_of_interest.py``. Otherwise it goes under whichever other status it had most often. A
concept whose mentions disagree (affirmed in the plan, negated in the review of systems) shows
the other counts after it, ``(also negated 1)``: those are the lines worth reading in the note.

A concept is titled by its preferred SNOMED CT, RxNorm, ICD-10-CM or LOINC display, in that
order, when it has one: an engine's preferred name can be any synonym ("EHT" for essential
hypertension). Without codes it keeps the engine's name.

``history_of`` is not shown. umlsmatch's is often wrong (README), and a past history is the
patient's anyway, so it does not move a concept between the lists.

Codes are the ones on the mentions: for notes, ``--codes`` (or ``code_systems`` in
cuiflow.toml) with a terminology database; for a run, whatever it was extracted with. Only each
system's preferred code is shown. ``--ingredients`` adds each RxNorm product's ingredients, so
"Tylenol 500 mg" also reads as acetaminophen. ``--mention-filter guidelines`` drops headings and
template words ("PLAN", "DISEASE"), which otherwise fill the lists.

Sources are notes (files, folders, ``.jsonl``), run directories from ``cuiflow extract
--out-dir``, or ``results.jsonl[.gz]`` files. A run is read as it is: no engine is loaded.

**Unassessed is not absent.** Under ``single:mmlite``, subject and hedging are never assessed,
so a family history lands under affirmed. The summary says which questions went unassessed.

Everything printed quotes concept names and note keys: treat it as PHI. ``--json`` also quotes
the words each concept was found as. A note that fails is reported by key and exception type,
never the message.

Exit status: 0 on success, 1 when any document failed, 2 on a usage or configuration error.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cuiflow import DocumentResult, Extractor, Mention, load_config
from cuiflow.core.enums import EngineMode
from cuiflow.core.manifests import MANIFEST_NAME
from cuiflow.io.readers import iter_documents
from cuiflow.io.writers.jsonl import iter_jsonl

RESULTS_NAME = "results.jsonl"  # what `cuiflow extract --out-dir` writes
STATUSES = ("affirmed", "negated", "other_subject", "hedged")
LABELS = {
    "affirmed": "affirmed",
    "negated": "negated",
    "other_subject": "someone else's",
    "hedged": "hedged",
}
#: Headings for the groups a clinician reads first; any other group follows, in name order.
GROUP_TITLES = {
    "DISORDER": "Problems",
    "FINDING": "Findings",
    "DRUG": "Medications",
    "PROCEDURE": "Procedures",
    "LAB": "Labs",
    "ANATOMY": "Anatomy",
}
#: The attributes that answer each question; the question is assessed when any is not None.
QUESTIONS = {
    "negation": ("negated",),
    "subject": ("subject",),
    "hedging": ("uncertain", "conditional", "generic", "temporality"),
}


def mention_status(m: Mention) -> str:
    """The first bucket a mention fits. ``None`` (not assessed) never excludes a mention."""
    a = m.assertions
    if a.negated:
        return "negated"
    if a.subject not in (None, "patient"):
        return "other_subject"
    if a.uncertain or a.conditional or a.generic or a.temporality == "hypothetical":
        return "hedged"
    return "affirmed"


#: Whose preferred-code display names a concept, in order. An engine's preferred name can be
#: any synonym ("EHT" for essential hypertension); a code's display is the vocabulary's own.
NAMING_SYSTEMS = ("SNOMEDCT_US", "RXNORM", "ICD10CM", "LNC")


@dataclass
class Concept:
    cui: str
    engine_name: str  # Mention.preferred_name
    group: str
    counts: Counter[str] = field(default_factory=Counter)
    texts: list[str] = field(default_factory=list)  # distinct surface forms, first seen first
    codes: dict[str, tuple[str, str]] = field(default_factory=dict)  # system -> (code, display)
    ingredients: set[str] = field(default_factory=set)  # RxCUIs

    @property
    def name(self) -> str:
        for system in NAMING_SYSTEMS:
            if system in self.codes:
                return self.codes[system][1]
        return self.engine_name

    def extra_ingredients(self) -> list[str]:
        """Ingredient RxCUIs other than the product's own code (an ingredient is its own)."""
        own = self.codes.get("RXNORM", ("", ""))[0]
        return sorted(self.ingredients - {own})

    @property
    def status(self) -> str:
        if self.counts["affirmed"]:
            return "affirmed"
        # Most frequent exclusion; ties go to the earlier bucket (negated before hedged).
        return max(STATUSES[1:], key=lambda s: (self.counts[s], -STATUSES.index(s)))

    def others(self) -> dict[str, int]:
        """Counts under the statuses this concept is not listed under."""
        return {s: self.counts[s] for s in STATUSES if s != self.status and self.counts[s]}


@dataclass
class Summary:
    doc_id: str
    concepts: list[Concept] = field(default_factory=list)
    n_mentions: int = 0
    unassessed: set[str] = field(default_factory=set)
    error_type: str | None = None


def summarize(result: DocumentResult, groups: set[str] | None = None) -> Summary:
    """One entry per CUI in ``result``, in order of first mention."""
    summary = Summary(result.doc_id)
    by_cui: dict[str, Concept] = {}
    for m in result.mentions:
        if groups and m.semantic_group.upper() not in groups:
            continue
        summary.n_mentions += 1
        c = by_cui.get(m.cui)
        if c is None:
            c = by_cui[m.cui] = Concept(m.cui, m.preferred_name, m.semantic_group)
        status = mention_status(m)
        c.counts[status] += 1
        if m.text not in c.texts:
            c.texts.append(m.text)
        for code in m.codes:
            if code.is_preferred:
                c.codes.setdefault(code.system, (code.code, code.display))
                c.ingredients.update(code.ingredients)
        if status == "affirmed":
            summary.unassessed |= {
                q
                for q, attrs in QUESTIONS.items()
                if all(getattr(m.assertions, n) is None for n in attrs)
            }
    summary.concepts = list(by_cui.values())
    return summary


# ---- sources ----------------------------------------------------------------------------------


def _is_results_file(path: Path) -> bool:
    """True when the first record of a .jsonl[.gz] file is a DocumentResult, not a note."""
    if not path.name.endswith((".jsonl", ".jsonl.gz")):
        return False
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return False
                return isinstance(record, dict) and "mentions" in record and "doc_id" in record
    return False


def iter_results(
    paths: list[Path], extractor: Any
) -> Iterator[tuple[str, DocumentResult | None, str | None]]:
    """``(doc_id, result, None)`` per document, or ``(doc_id, None, error_type)`` when it failed.

    ``extractor`` is called on first use only, so reading a run never loads an engine.
    """
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(f"no such file or folder: {path}")
        run = path.is_dir() and (path / RESULTS_NAME).is_file()
        if run or (path.is_file() and _is_results_file(path)):
            seen: set[str] = set()
            for result in iter_jsonl(path / RESULTS_NAME if run else path):
                seen.add(result.doc_id)
                yield result.doc_id, result, None
            manifest = path / MANIFEST_NAME
            if run and manifest.is_file():
                for err in json.loads(manifest.read_text(encoding="utf-8")).get("errors", ()):
                    if err["doc_key"] not in seen:  # a failure fixed by --resume is in results
                        seen.add(err["doc_key"])
                        yield err["doc_key"], None, err["error_type"]
            continue
        for doc in iter_documents([path]):
            try:
                yield doc.doc_id, extractor().process(doc.text, doc.doc_id), None
            except Exception as exc:  # one bad note must not end the run
                yield doc.doc_id, None, type(exc).__name__


# ---- output -----------------------------------------------------------------------------------


def group_order(group: str) -> tuple[int, str]:
    titles = list(GROUP_TITLES)
    return (titles.index(group), "") if group in titles else (len(titles), group)


def format_summary(s: Summary, *, show_codes: bool = True) -> list[str]:
    if s.error_type is not None:
        return [f"== {s.doc_id}", f"   could not be processed: {s.error_type}", ""]
    lines = [f"== {s.doc_id}  ({s.n_mentions} mentions, {len(s.concepts)} concepts)"]
    if not s.concepts:
        lines += ["   no concepts found", ""]
        return lines
    for group in sorted({c.group for c in s.concepts}, key=group_order):
        lines.append(f"   {GROUP_TITLES.get(group, group.title())} ({group})")
        in_group = [c for c in s.concepts if c.group == group]
        for status in STATUSES:
            listed = [c for c in in_group if c.status == status]
            if not listed:
                continue
            for n, c in enumerate(listed):
                label = f"{LABELS[status]}:" if n == 0 else ""
                line = f"     {label:<16} {c.name}"
                if sum(c.counts.values()) > 1:
                    line += f" x{sum(c.counts.values())}"
                if others := c.others():
                    shown = ", ".join(f"{LABELS[k]} {v}" for k, v in others.items())
                    line += f"  (also {shown})"
                if show_codes and c.codes:
                    line += "  [" + ", ".join(f"{k} {v[0]}" for k, v in sorted(c.codes.items()))
                    if ingredients := c.extra_ingredients():
                        line += f"; ingredient RXNORM {', '.join(ingredients)}"
                    line += "]"
                lines.append(line)
    if s.unassessed:
        lines.append(
            f"   note: {', '.join(sorted(s.unassessed))} not assessed on some affirmed concepts"
        )
    lines.append("")
    return lines


def to_json(s: Summary) -> dict[str, Any]:
    if s.error_type is not None:
        return {"doc_id": s.doc_id, "error_type": s.error_type}
    return {
        "doc_id": s.doc_id,
        "n_mentions": s.n_mentions,
        "unassessed": sorted(s.unassessed),
        "concepts": [
            {
                "cui": c.cui,
                "name": c.name,
                "engine_name": c.engine_name,
                "group": c.group,
                "status": c.status,
                "counts": {k: c.counts[k] for k in STATUSES},
                "texts": c.texts,
                "codes": {k: {"code": v[0], "display": v[1]} for k, v in sorted(c.codes.items())},
                "ingredients": sorted(c.ingredients),
            }
            for c in s.concepts
        ],
    }


# ---- main -------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "sources",
        nargs="+",
        type=Path,
        help="notes (files, folders, .jsonl), run directories, or results.jsonl files",
    )
    ap.add_argument("--groups", nargs="+", metavar="GROUP", help="only these semantic groups")
    ap.add_argument("--limit", type=int, help="stop after this many documents")
    ap.add_argument("--json", action="store_true", help="one JSON object per document (JSONL)")
    ap.add_argument("--no-codes", action="store_true", help="leave codes out of the text report")
    cfg = ap.add_argument_group("extraction of notes (a run keeps its own settings)")
    cfg.add_argument("--config", type=Path, help="config file (default: ./cuiflow.toml)")
    cfg.add_argument("--mode", choices=[m.value for m in EngineMode])
    cfg.add_argument("--codes", help="comma-separated code systems, e.g. SNOMEDCT_US,RXNORM")
    cfg.add_argument("--ingredients", action="store_true", help="add RxNorm ingredients")
    cfg.add_argument("--mention-filter", choices=["none", "guidelines"])
    cfg.add_argument("--terminology-db", type=Path)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be at least 1")
    groups = {g.upper() for g in args.groups} if args.groups else None
    flags: dict[str, Any] = {
        "mode": args.mode,
        "terminology_db": args.terminology_db,
        "mention_filter": args.mention_filter,
    }
    if args.codes:
        flags["code_systems"] = [s.strip().upper() for s in args.codes.split(",") if s.strip()]
    if args.ingredients:
        flags["include_ingredients"] = True

    counts: Counter[str] = Counter()
    with ExitStack() as stack:
        loaded: list[Extractor] = []

        def extractor() -> Extractor:
            if not loaded:
                config = load_config(
                    {k: v for k, v in flags.items() if v is not None}, config_file=args.config
                )
                loaded.append(stack.enter_context(Extractor(config)))
            return loaded[0]

        # Messages caught here come from paths and configuration, never from note text:
        # errors on a note are caught per note in iter_results().
        try:
            for n, (doc_id, result, error_type) in enumerate(iter_results(args.sources, extractor)):
                if args.limit is not None and n >= args.limit:
                    break
                if result is None:
                    s = Summary(doc_id, error_type=error_type)
                else:
                    s = summarize(result, groups)
                counts["error" if error_type else "ok"] += 1
                if args.json:
                    print(json.dumps(to_json(s), ensure_ascii=False))
                else:
                    print("\n".join(format_summary(s, show_codes=not args.no_codes)))
        except (OSError, ValueError, ImportError, NotImplementedError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except KeyError as exc:  # a .jsonl record without "text", or a malformed manifest
            print(f"error: missing field {exc} in an input record", file=sys.stderr)
            return 2

    total = counts["ok"] + counts["error"]
    if not total:
        print("no documents found", file=sys.stderr)
        return 1
    sys.stdout.flush()
    print(f"summarized {counts['ok']} of {total} documents", file=sys.stderr)
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
