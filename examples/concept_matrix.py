#!/usr/bin/env python
"""Build a document x concept feature matrix, with each cell saying how the note asserts it.

Usage:
    python examples/concept_matrix.py out/runs/<run-id> -o features.csv
    python examples/concept_matrix.py out/runs/<run-id> -o features.csv --groups DISORDER DRUG
    python examples/concept_matrix.py synthetic/ -o features.csv --min-docs 5 --top 30

One row per document, one column per concept (CUI). A cell is

    1   affirmed: some mention is not negated, about the patient and not hedged
   -1   mentioned, but only negated, about someone else, or hedged
    0   not mentioned

the same rule as ``notes_of_interest.py``. Keeping "denies chest pain" apart from "no mention of
chest pain" is the point: a bag-of-CUIs matrix counts both as chest pain or both as nothing.
Collapse -1 to 0 if a model only needs presence.

Every document that was processed gets a row, **including those with no concept at all**: a
matrix that drops them changes every rate computed from it. A document that failed is left out
of the matrix, listed on stderr by key and exception type, and makes the exit status 1, so it is
never mistaken for a note that mentions nothing.

Alongside ``features.csv`` goes ``features.concepts.csv``, one row per column: CUI, preferred
name, semantic group, and how many documents affirm it and mention it without affirming it. The
report on stdout ranks the same numbers, which is often the first thing to look at in a corpus:
the concepts most often mentioned but not affirmed are what a keyword count would get wrong.

``--min-docs N`` keeps the concepts affirmed in at least N documents (default 1); rare concepts
are most of the columns and little of the signal. ``--groups`` keeps only those semantic groups.

Sources are notes (files, folders, ``.jsonl``), run directories from ``cuiflow extract
--out-dir``, or ``results.jsonl[.gz]`` files. Extract once and build matrices from the run: it is
read as it is, with no engine loaded.

**Unassessed is not absent.** Under ``single:mmlite`` subject and hedging are never assessed,
so a family history is a 1. The report says which questions went unassessed.

The matrix names documents by key, usually the file path: treat it as PHI.

Exit status: 0 on success, 1 when any document failed, 2 on a usage or configuration error.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from cuiflow import DocumentResult, Extractor, Mention, load_config
from cuiflow.core.enums import EngineMode
from cuiflow.core.manifests import MANIFEST_NAME
from cuiflow.io.readers import iter_documents
from cuiflow.io.writers.jsonl import iter_jsonl

RESULTS_NAME = "results.jsonl"  # what `cuiflow extract --out-dir` writes
AFFIRMED, NOT_AFFIRMED, ABSENT = 1, -1, 0
#: Whose preferred-code display names a concept, in order: an engine's preferred name can be any
#: synonym ("EHT" for essential hypertension). Mentions extracted without codes keep it.
NAMING_SYSTEMS = ("SNOMEDCT_US", "RXNORM", "ICD10CM", "LNC")
QUESTIONS = {
    "negation": ("negated",),
    "subject": ("subject",),
    "hedging": ("uncertain", "conditional", "generic", "temporality"),
}


def is_affirmed(m: Mention) -> bool:
    """Not negated, about the patient, not hedged. ``None`` (not assessed) never excludes."""
    a = m.assertions
    return not (
        a.negated
        or a.subject not in (None, "patient")
        or a.uncertain
        or a.conditional
        or a.generic
        or a.temporality == "hypothetical"
    )


def display_name(m: Mention) -> str:
    preferred = {c.system: c.display for c in m.codes if c.is_preferred}
    return next((preferred[s] for s in NAMING_SYSTEMS if s in preferred), m.preferred_name)


@dataclass
class ConceptStats:
    name: str
    group: str
    n_affirmed: int = 0  # documents
    n_not_affirmed: int = 0  # documents that mention it, never affirmed


@dataclass
class Matrix:
    rows: dict[str, dict[str, int]] = field(default_factory=dict)  # doc_id -> {cui: 1 | -1}
    concepts: dict[str, ConceptStats] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (doc_id, error type)
    unassessed: set[str] = field(default_factory=set)
    duplicates: int = 0

    def add(self, result: DocumentResult, groups: set[str] | None = None) -> None:
        cells: dict[str, int] = {}
        for m in result.mentions:
            if groups and m.semantic_group.upper() not in groups:
                continue
            if m.cui not in self.concepts:
                self.concepts[m.cui] = ConceptStats(display_name(m), m.semantic_group)
            if is_affirmed(m):
                cells[m.cui] = AFFIRMED
                self.unassessed |= {
                    q
                    for q, attrs in QUESTIONS.items()
                    if all(getattr(m.assertions, n) is None for n in attrs)
                }
            else:
                cells.setdefault(m.cui, NOT_AFFIRMED)
        if result.doc_id in self.rows:
            # Two documents with one key would overwrite each other's row: keep both apart.
            self.duplicates += 1
            key = f"{result.doc_id}#{self.duplicates}"
        else:
            key = result.doc_id
        self.rows[key] = cells
        for cui, value in cells.items():
            if value == AFFIRMED:
                self.concepts[cui].n_affirmed += 1
            else:
                self.concepts[cui].n_not_affirmed += 1

    def columns(self, min_docs: int) -> list[str]:
        """CUIs affirmed in at least ``min_docs`` documents, most often affirmed first."""
        kept = [c for c, s in self.concepts.items() if s.n_affirmed >= min_docs]
        return sorted(kept, key=lambda c: (-self.concepts[c].n_affirmed, c))


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


def write_matrix(path: Path, matrix: Matrix, columns: list[str]) -> Path:
    """Write the matrix and its concept dictionary; returns the dictionary's path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["doc_id", *columns])
        for doc_id, cells in matrix.rows.items():
            w.writerow([doc_id, *(cells.get(c, ABSENT) for c in columns)])
    concepts_path = path.with_name(f"{path.stem}.concepts.csv")
    with concepts_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["cui", "preferred_name", "semantic_group", "n_affirmed", "n_not_affirmed"])
        for cui in columns:
            s = matrix.concepts[cui]
            w.writerow([cui, s.name, s.group, s.n_affirmed, s.n_not_affirmed])
    return concepts_path


def report(matrix: Matrix, columns: list[str], top: int) -> list[str]:
    n = len(matrix.rows)
    empty = sum(1 for cells in matrix.rows.values() if not cells)
    lines = [
        f"{n} documents x {len(columns)} concepts "
        f"({len(matrix.concepts)} seen; {len(matrix.concepts) - len(columns)} below --min-docs)",
    ]
    if empty:
        lines.append(f"{empty} documents have no concept; they are all-zero rows, not dropped")
    if not columns or not top:
        return lines

    def table(title: str, cuis: list[str]) -> None:
        lines.extend(["", title, f"  {'affirmed':>8} {'not':>5}  concept"])
        for cui in cuis[:top]:
            s = matrix.concepts[cui]
            lines.append(f"  {s.n_affirmed:>8} {s.n_not_affirmed:>5}  {cui} {s.name} ({s.group})")

    table("Most often affirmed (documents):", columns)
    by_not = sorted(
        (c for c in columns if matrix.concepts[c].n_not_affirmed),
        key=lambda c: (-matrix.concepts[c].n_not_affirmed, c),
    )
    if by_not:
        table("Most often mentioned without being affirmed (documents):", by_not)
    return lines


# ---- main -------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "sources",
        nargs="+",
        type=Path,
        help="notes (files, folders, .jsonl), run directories, or results.jsonl files",
    )
    ap.add_argument("-o", "--output", type=Path, required=True, help="the matrix, as CSV")
    ap.add_argument("--groups", nargs="+", metavar="GROUP", help="only these semantic groups")
    ap.add_argument(
        "--min-docs", type=int, default=1, help="keep concepts affirmed in >= N documents"
    )
    ap.add_argument("--top", type=int, default=15, help="concepts to list in the report (0: none)")
    cfg = ap.add_argument_group("extraction of notes (a run keeps its own settings)")
    cfg.add_argument("--config", type=Path, help="config file (default: ./cuiflow.toml)")
    cfg.add_argument("--mode", choices=[m.value for m in EngineMode])
    cfg.add_argument("--mention-filter", choices=["none", "guidelines"])
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.min_docs < 1:
        ap.error("--min-docs must be at least 1")
    if args.top < 0:
        ap.error("--top must not be negative")
    groups = {g.upper() for g in args.groups} if args.groups else None
    flags = {"mode": args.mode, "mention_filter": args.mention_filter}

    matrix = Matrix()
    with ExitStack() as stack:
        loaded: list[Extractor] = []

        def extractor() -> Extractor:
            if not loaded:
                config = load_config(
                    {k: v for k, v in flags.items() if v is not None}, config_file=args.config
                )
                # Matching is by CUI: code lookups would only slow every note down.
                config = replace(
                    config, code_systems=(), ancestor_depth=0, include_ingredients=False
                )
                loaded.append(stack.enter_context(Extractor(config)))
            return loaded[0]

        # Messages caught here come from paths and configuration, never from note text:
        # errors on a note are caught per note in iter_results().
        try:
            for doc_id, result, error_type in iter_results(args.sources, extractor):
                if result is None:
                    matrix.errors.append((doc_id, error_type or "Exception"))
                else:
                    matrix.add(result, groups)
        except (OSError, ValueError, ImportError, NotImplementedError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except KeyError as exc:  # a .jsonl record without "text", or a malformed manifest
            print(f"error: missing field {exc} in an input record", file=sys.stderr)
            return 2

    if not matrix.rows and not matrix.errors:
        print("no documents found", file=sys.stderr)
        return 1
    columns = matrix.columns(args.min_docs)
    try:
        concepts_path = write_matrix(args.output, matrix, columns)
    except OSError as exc:
        print(f"error: could not write {args.output}: {exc}", file=sys.stderr)
        return 2

    print("\n".join(report(matrix, columns, args.top)))
    if matrix.unassessed:
        print(
            f"\nnote: {', '.join(sorted(matrix.unassessed))} went unassessed on some affirmed "
            "mentions; this mode cannot tell those apart, so some 1s may not be affirmed."
        )
    sys.stdout.flush()
    if matrix.duplicates:
        print(
            f"note: {matrix.duplicates} document key(s) repeat; later rows got a #N suffix",
            file=sys.stderr,
        )
    if matrix.errors:
        print(f"\n{len(matrix.errors)} document(s) could not be processed:", file=sys.stderr)
        for doc_id, error_type in sorted(matrix.errors):
            print(f"  {doc_id}  {error_type}", file=sys.stderr)
    print(f"-> {args.output}, {concepts_path}", file=sys.stderr)
    return 1 if matrix.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
