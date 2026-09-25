#!/usr/bin/env python
"""Find the notes that affirm a clinical concept: simple phenotyping on top of cuiflow.

Usage:
    python examples/notes_of_interest.py synthetic/ --term "chest pain"
    python examples/notes_of_interest.py synthetic/ --cui C0011849 C0011860 --show-excluded
    python examples/notes_of_interest.py out/runs/<run-id> --code ICD10CM:I21
    python examples/notes_of_interest.py synthetic/ --term "chest pain" --save cohort.sqlite

**Why this needs NLP and not grep.** A keyword search for "chest pain" also matches "denies
chest pain", "mother had chest pain" and "rule out chest pain". A note counts here only when a
target mention is **affirmed**: not negated, about the patient, and not hedged (uncertain,
conditional, hypothetical or generic). A past history ("history of MI") is affirmed: it is still
the patient's. ``--show-excluded`` lists the notes that mention the concept only in some other
way, so you can see how often each distinction fires.

Concepts are matched by CUI, so synonyms and abbreviations ("type 2 diabetes", "T2DM") collapse
to one concept. That is the other thing grep cannot do. There are three ways to name targets:

- ``--term PHRASE`` runs the phrase through the extractor, so it resolves with the same engine
  and settings that found the mentions. Only the longest spans become targets ("chest pain" is
  *Chest Pain*, not also the *Chest* and *Pain* inside it); the rest are printed as not searched.
  The resolved concepts are printed before the scan: a phrase that resolves to something you
  did not mean is the commonest way a cohort goes wrong.
- ``--cui CUI ...`` when you already know the concepts.
- ``--code SYSTEM:CODE ...`` takes every CUI filed under that code **and every code below it** in
  the hierarchy, so ``ICD10CM:I21`` also finds notes whose concept is only coded as ``I21.4``.
  Only ICD-10-CM and SNOMED CT have a hierarchy; other systems match the code itself.
  Needs a terminology database (``--terminology-db`` or ``CUIFLOW_TERMINOLOGY_DB``), built with
  ``--mrrel`` for the descendants.

Sources are notes (files, folders, ``.jsonl``: anything ``cuiflow extract`` reads), run
directories from ``cuiflow extract --out-dir``, or ``results.jsonl[.gz]`` files. A run is read
as it is, with no engine loaded unless ``--term`` needs one, so on a large corpus: extract once,
query as often as you like. ``--term`` then resolves with **the run's recorded configuration**
(mode, engine paths, filters), because another mode would resolve the phrase to other CUIs.
Documents a run failed on are reported as errors, not silently dropped from the denominator.

**Unassessed is not absent.** An assertion a mode does not assess is ``None``: mmlite never
assesses ``subject``, and NegEx assesses nothing but ``negated``, so under ``single:mmlite`` a
family-history or "possible" mention counts as affirmed. The report says which of negation,
subject and hedging went unassessed on affirmed mentions.

``--save PATH`` **appends** the search to SQLite, so one file accumulates a history of what was
asked: ``search`` (one row per run), ``concepts`` (the targets), ``documents`` (every document,
**including those with no mention or an error**: a cohort denominator cannot be rebuilt from a
list of hits) and ``console`` (the printed report, verbatim). Nothing is written when the search
fails early, so a ``search`` row always means a search that ran.

Every output names documents by their key, usually the file path: treat it as PHI. Errors on a
note name only the key and the exception type, never the message, which can quote the note.

Exit status: 0 on success, 1 when the search ran but some documents failed or nothing matched
the query, 2 on a usage or configuration error.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cuiflow import DocumentResult, ExtractionConfig, Extractor, Mention, load_config
from cuiflow.core.alignment import longest_non_overlapping
from cuiflow.core.enums import EngineMode
from cuiflow.core.manifests import MANIFEST_NAME
from cuiflow.io.readers import iter_documents
from cuiflow.io.writers.jsonl import iter_jsonl
from cuiflow.terminology.hierarchy import HIERARCHY_SOURCES
from cuiflow.terminology.store import TerminologyStore

RESULTS_NAME = "results.jsonl"  # what `cuiflow extract --out-dir` writes
CUI_RE = re.compile(r"C\d{7}")
#: The attributes that can exclude a mention, by the question they answer. A question counts as
#: assessed when any of its attributes is not None: umlsmatch answers "hedged" with
#: uncertain/conditional/generic and ConText with temporality, and neither fills the others.
QUESTIONS = {
    "negation": ("negated",),
    "subject": ("subject",),
    "hedging": ("uncertain", "conditional", "generic", "temporality"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS search (
    search_id   INTEGER PRIMARY KEY,
    ran_at      TEXT NOT NULL,
    query       TEXT NOT NULL,      -- e.g. "term:chest pain", "code:ICD10CM:I21"
    cuis        TEXT NOT NULL,      -- comma-joined targets
    sources     TEXT NOT NULL,
    mode        TEXT,               -- the notes' mode; NULL when only runs were queried
    n_documents INTEGER NOT NULL,   -- n_affirmed + n_excluded + n_absent + n_errors
    n_affirmed  INTEGER NOT NULL,
    n_excluded  INTEGER NOT NULL,   -- mentioned, but never affirmed
    n_absent    INTEGER NOT NULL,
    n_errors    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS concepts (
    search_id      INTEGER NOT NULL REFERENCES search(search_id),
    cui            TEXT NOT NULL,
    preferred_name TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    search_id       INTEGER NOT NULL REFERENCES search(search_id),
    doc_id          TEXT NOT NULL,      -- the document key (PHI)
    status          TEXT NOT NULL,      -- 'affirmed' | 'excluded' | 'absent' | 'error'
    n_affirmed      INTEGER NOT NULL,
    n_negated       INTEGER NOT NULL,
    n_other_subject INTEGER NOT NULL,
    n_hedged        INTEGER NOT NULL DEFAULT 0,  -- uncertain, conditional, hypothetical, generic
    error_type      TEXT                         -- exception class only, never its message
);

CREATE TABLE IF NOT EXISTS console (
    search_id INTEGER NOT NULL REFERENCES search(search_id),
    line_no   INTEGER NOT NULL,
    line      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_doc_search ON documents(search_id, status);
CREATE INDEX IF NOT EXISTS ix_con_search ON concepts(search_id, cui);
"""


class Console:
    """Print to stdout and keep a verbatim transcript for the ``console`` table."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        print(text)
        self.lines.extend(text.split("\n"))


def warn(text: str) -> None:
    print(f"note: {text}", file=sys.stderr)


# ---- classification ---------------------------------------------------------------------------


@dataclass
class DocumentStatus:
    doc_id: str
    n_affirmed: int = 0
    n_negated: int = 0
    n_other_subject: int = 0
    n_hedged: int = 0
    error_type: str | None = None
    unassessed: set[str] = field(default_factory=set)  # QUESTIONS no affirmed mention answered

    @property
    def status(self) -> str:
        if self.error_type is not None:
            return "error"
        if self.n_affirmed:
            return "affirmed"
        return "excluded" if self.n_negated or self.n_other_subject or self.n_hedged else "absent"


def is_hedged(m: Mention) -> bool:
    a = m.assertions
    return bool(a.uncertain or a.conditional or a.generic or a.temporality == "hypothetical")


def classify(result: DocumentResult, targets: set[str]) -> DocumentStatus:
    """Count the target mentions in one document by how they are asserted.

    Each mention lands in the first bucket it fits: negated, about someone else, hedged, else
    affirmed. ``None`` (not assessed) never excludes a mention; it is recorded instead.
    """
    doc = DocumentStatus(result.doc_id)
    for m in result.mentions:
        if m.cui not in targets:
            continue
        a = m.assertions
        if a.negated:
            doc.n_negated += 1
        elif a.subject not in (None, "patient"):
            doc.n_other_subject += 1
        elif is_hedged(m):
            doc.n_hedged += 1
        else:
            doc.n_affirmed += 1
            doc.unassessed |= {
                q for q, attrs in QUESTIONS.items() if all(getattr(a, n) is None for n in attrs)
            }
    return doc


# ---- sources ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    path: Path
    kind: str  # "run" | "results" | "notes"

    @property
    def results(self) -> Path:
        return self.path / RESULTS_NAME if self.kind == "run" else self.path


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


def classify_source(path: Path) -> Source:
    if path.is_dir():
        return Source(path, "run" if (path / RESULTS_NAME).is_file() else "notes")
    if not path.is_file():
        raise FileNotFoundError(f"no such file or folder: {path}")
    return Source(path, "results" if _is_results_file(path) else "notes")


def run_manifest(source: Source) -> dict[str, Any]:
    """The run's manifest, or {} for a bare results file or a run without one."""
    if source.kind != "run" or not (source.path / MANIFEST_NAME).is_file():
        return {}
    manifest: dict[str, Any] = json.loads((source.path / MANIFEST_NAME).read_text(encoding="utf-8"))
    return manifest


def scan(
    sources: Iterable[Source],
    targets: set[str],
    extractor: Callable[[], Extractor],
) -> Iterator[DocumentStatus]:
    """One status per document: runs as recorded (plus their errors), notes by extracting.

    A note that fails is an ``error`` row, never a crash and never a silent gap: the report
    must account for every document it was given.
    """
    for source in sources:
        if source.kind == "notes":
            for doc in iter_documents([source.path]):
                try:
                    result = extractor().process(doc.text, doc.doc_id)
                except Exception as exc:  # one bad note must not end the scan
                    yield DocumentStatus(doc.doc_id, error_type=type(exc).__name__)
                    continue
                yield classify(result, targets)
            continue
        seen: set[str] = set()
        for result in iter_jsonl(source.results):
            seen.add(result.doc_id)
            yield classify(result, targets)
        # A document that failed and then succeeded on --resume is in the results: skip it.
        for err in run_manifest(source).get("errors", ()):
            if err["doc_key"] not in seen:
                seen.add(err["doc_key"])
                yield DocumentStatus(err["doc_key"], error_type=err["error_type"])


# ---- resolving the query ----------------------------------------------------------------------


def resolve_term(term: str, extractor: Extractor) -> tuple[list[Mention], list[Mention]]:
    """``(targets, dropped)``: the longest spans the phrase resolves to, and the nested rest."""
    mentions = list(extractor.process(term, "query").mentions)
    targets = longest_non_overlapping(mentions)
    kept = {m.cui for m in targets}
    dropped: dict[str, Mention] = {}
    for m in mentions:
        if m.cui not in kept:
            dropped.setdefault(m.cui, m)
    return targets, list(dropped.values())


def resolve_codes(codes: list[str], store: TerminologyStore) -> dict[str, list[str]]:
    """``{CUI: [codes it was reached from]}`` for each code and every code below it."""
    known = set(store.build_info().get("sources", ()))
    found: dict[str, list[str]] = {}
    for spec in codes:
        system, sep, code = spec.partition(":")
        system, code = system.strip().upper(), code.strip().upper()
        if not sep or not system or not code:
            raise ValueError(f"--code wants SYSTEM:CODE (e.g. ICD10CM:I21), got {spec!r}")
        if known and system not in known:
            raise ValueError(f"{store.path} has no {system} codes; it holds {', '.join(known)}")
        if system in HIERARCHY_SOURCES and system not in store.hierarchy_sources:
            warn(f"{store.path} has no {system} hierarchy: {spec} matches itself only")
        own = store.cuis_for_code(system, code)
        if not own:
            warn(f"{system}:{code} is not in {store.path}")
        for cui in own:
            found.setdefault(cui, []).append(f"{system}:{code}")
        for descendant, _ in store.descendants(system, code):
            for cui in store.cuis_for_code(system, descendant):
                found.setdefault(cui, []).append(f"{system}:{descendant}")
    return found


def effective_config(args: argparse.Namespace, sources: list[Source]) -> ExtractionConfig:
    """Flags over the first run's recorded config (if any) over CUIFLOW_* and cuiflow.toml.

    Codes are dropped: matching is by CUI, and code lookups would only slow every note down.
    """
    given = {"mode": args.mode, "terminology_db": args.terminology_db}
    flags = {k: v for k, v in given.items() if v is not None}  # unset must not erase the run's
    configs = [m["config"] for s in sources if (m := run_manifest(s)) and "config" in m]
    modes = {c.get("mode") for c in configs}
    if len(modes) > 1:
        warn(f"the runs were made with different modes ({', '.join(sorted(map(str, modes)))})")
    recorded = dict(configs[0]) if configs else {}
    config = load_config({**recorded, **flags}, config_file=args.config)
    if configs and any(s.kind == "notes" for s in sources) and config.mode.value not in modes:
        warn(f"notes are extracted with {config.mode.value}, the runs used {', '.join(modes)}")
    if recorded and args.mode and args.mode != recorded.get("mode") and args.term:
        warn(f"--term resolves with {args.mode}; the run was made with {recorded.get('mode')}")
    return replace(config, code_systems=(), ancestor_depth=0, include_ingredients=False)


# ---- main -------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "sources",
        nargs="+",
        type=Path,
        help="notes (files, folders, .jsonl), run directories, or results.jsonl files",
    )
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--term", help="a phrase to resolve to concepts, then search for")
    target.add_argument("--cui", nargs="+", help="search for these CUIs")
    target.add_argument(
        "--code", nargs="+", metavar="SYSTEM:CODE", help="search for a code and everything below it"
    )
    ap.add_argument(
        "--show-excluded",
        action="store_true",
        help="also list the notes that mention it but never affirm it",
    )
    ap.add_argument("--save", type=Path, help="append this search to a SQLite file")
    cfg = ap.add_argument_group("configuration (overrides a run's config, CUIFLOW_*, cuiflow.toml)")
    cfg.add_argument("--config", type=Path, help="config file (default: ./cuiflow.toml)")
    cfg.add_argument("--mode", choices=[m.value for m in EngineMode])
    cfg.add_argument("--terminology-db", type=Path, help="needed by --code")
    return ap


def save_search(
    path: Path,
    *,
    query: str,
    names: dict[str, str],
    sources: list[Source],
    mode: str | None,
    docs: list[DocumentStatus],
    console: list[str],
) -> int:
    counts = Counter(d.status for d in docs)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        cur = conn.execute(
            "INSERT INTO search (ran_at, query, cuis, sources, mode, n_documents, n_affirmed, "
            "n_excluded, n_absent, n_errors) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(UTC).isoformat(timespec="seconds"),
                query,
                ",".join(sorted(names)),
                ";".join(str(s.path) for s in sources),
                mode,
                len(docs),
                counts["affirmed"],
                counts["excluded"],
                counts["absent"],
                counts["error"],
            ),
        )
        search_id = cur.lastrowid
        assert search_id is not None
        conn.executemany(
            "INSERT INTO concepts (search_id, cui, preferred_name) VALUES (?, ?, ?)",
            [(search_id, cui, name or None) for cui, name in sorted(names.items())],
        )
        conn.executemany(
            "INSERT INTO documents (search_id, doc_id, status, n_affirmed, n_negated, "
            "n_other_subject, n_hedged, error_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    search_id,
                    d.doc_id,
                    d.status,
                    d.n_affirmed,
                    d.n_negated,
                    d.n_other_subject,
                    d.n_hedged,
                    d.error_type,
                )
                for d in docs
            ],
        )
        conn.executemany(
            "INSERT INTO console (search_id, line_no, line) VALUES (?, ?, ?)",
            [(search_id, i, line) for i, line in enumerate(console, 1)],
        )
        conn.commit()
    finally:
        conn.close()
    return search_id


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cui:
        bad = [c for c in args.cui if not CUI_RE.fullmatch(c.strip().upper())]
        if bad:
            ap.error(f"not a CUI (C followed by 7 digits): {', '.join(bad)}")

    say = Console()
    started = time.perf_counter()
    with ExitStack() as stack:
        loaded: list[Extractor] = []

        def extractor() -> Extractor:
            # Loaded on first use, once: a run queried by --cui or --code never needs one.
            if not loaded:
                loaded.append(stack.enter_context(Extractor(config)))
            return loaded[0]

        # Messages caught here come from paths and configuration, never from note text:
        # errors on a note are caught per note in scan().
        try:
            sources = [classify_source(p) for p in args.sources]
            config = effective_config(args, sources)
            if args.term:
                mentions, dropped = resolve_term(args.term, extractor())
                if not mentions:
                    print(
                        f"{args.term!r} resolves to no concept. Try another wording, or --cui.",
                        file=sys.stderr,
                    )
                    return 1
                names = {m.cui: m.preferred_name for m in mentions}
                say(f"{args.term!r} resolves to {len(names)} concept(s):")
                for m in mentions:
                    say(f"  {m.cui}  {m.semantic_group:<10} {m.preferred_name}  ({m.text!r})")
                if dropped:
                    shown = ", ".join(f"{m.cui} {m.preferred_name}" for m in dropped)
                    say(f"  (narrower matches not searched: {shown}; add them with --cui)")
                query = f"term:{args.term}"
            elif args.code:
                if config.terminology_db is None:
                    ap.error("--code needs --terminology-db (or CUIFLOW_TERMINOLOGY_DB)")
                with TerminologyStore(config.terminology_db) as store:
                    reached = resolve_codes(args.code, store)
                    names = {cui: store.preferred_display(cui) or "" for cui in reached}
                if not reached:
                    print("no concept is filed under those codes", file=sys.stderr)
                    return 1
                say(f"{', '.join(args.code)} and the codes below cover {len(reached)} concept(s):")
                for cui in sorted(reached):
                    via = reached[cui]
                    shown = ", ".join(via[:3]) + (f", +{len(via) - 3}" if len(via) > 3 else "")
                    say(f"  {cui}  {names[cui]}  [{shown}]")
                query = f"code:{','.join(args.code)}"
            else:
                names = {c.strip().upper(): "" for c in args.cui}
                say(f"Searching for CUIs: {', '.join(sorted(names))}")
                query = f"cui:{','.join(sorted(names))}"

            # Every document, including those with no mention: "absent" is a result too.
            docs = list(scan(sources, set(names), extractor))
        except (OSError, ValueError, ImportError, NotImplementedError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except sqlite3.Error as exc:  # an unreadable terminology database
            print(f"error: terminology database: {exc}", file=sys.stderr)
            return 2
        except KeyError as exc:  # a .jsonl record without "text", or a malformed manifest
            print(f"error: missing field {exc} in an input record", file=sys.stderr)
            return 2

    if not docs:
        print("no documents found", file=sys.stderr)
        return 1

    by_status: dict[str, list[DocumentStatus]] = {}
    for d in docs:
        by_status.setdefault(d.status, []).append(d)
    affirmed = by_status.get("affirmed", [])
    excluded = by_status.get("excluded", [])
    errors = by_status.get("error", [])
    say(f"\nScanned {len(docs)} documents in {time.perf_counter() - started:.1f}s.")
    say(f"  {len(affirmed)} affirm the concept")
    say(f"  {len(excluded)} mention it but never affirm it (negated, someone else's, hedged)")
    say(f"  {len(by_status.get('absent', []))} do not mention it")
    if errors:
        say(f"  {len(errors)} could not be processed")

    if affirmed:
        say("\nAffirmed in:")
        for d in sorted(affirmed, key=lambda d: (-d.n_affirmed, d.doc_id)):
            say(f"  {d.doc_id:<52} {d.n_affirmed} mention(s)")
        unassessed = sorted(set().union(*(d.unassessed for d in affirmed)))
        if unassessed:
            say(
                f"\nnote: some affirmed mentions were not assessed for {', '.join(unassessed)}, "
                "so they may not all be affirmed (this mode does not detect it)."
            )
    if excluded and args.show_excluded:
        say("\nMentioned but not affirmed:")
        for d in sorted(excluded, key=lambda d: d.doc_id):
            say(
                f"  {d.doc_id:<52} negated {d.n_negated}, someone else's {d.n_other_subject}, "
                f"hedged {d.n_hedged}"
            )
    elif excluded:
        say(f"\n({len(excluded)} documents mention it without affirming it; --show-excluded)")
    if errors:
        say("\nCould not be processed:")
        for d in sorted(errors, key=lambda d: d.doc_id):
            say(f"  {d.doc_id:<52} {d.error_type}")

    duplicates = [k for k, n in Counter(d.doc_id for d in docs).items() if n > 1]
    if duplicates:
        warn(f"{len(duplicates)} document key(s) appear more than once and are counted each time")

    if args.save is not None:
        try:
            search_id = save_search(
                args.save,
                query=query,
                names=names,
                sources=sources,
                mode=config.mode.value if any(s.kind == "notes" for s in sources) else None,
                docs=docs,
                console=say.lines,
            )
        except sqlite3.Error as exc:
            print(f"error: could not save to {args.save}: {exc}", file=sys.stderr)
            return 2
        # Flush stdout first, or a redirected (block-buffered) report lands after this line.
        sys.stdout.flush()
        print(f"\n-> saved as search {search_id} in {args.save}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
