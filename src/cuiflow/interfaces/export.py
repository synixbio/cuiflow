"""Convert a run's results to another format (DESIGN_PLAN §8).

A batch run always writes ``results.jsonl``: it is what a killed run resumes from. Parquet,
SQLite and OMOP ``NOTE_NLP`` are made from it afterwards, streaming one document at a time, so
an export never needs the corpus in memory either::

    cuiflow export out/runs/<run_id> --format parquet -o mentions.parquet
    cuiflow export out/runs/<run_id> --format omop -o note_nlp.csv \\
        --note-ids note_ids.csv --omop-vocab data/omop_vocab.sqlite

Each export writes ``<output>.export.json`` beside its output: the source and its checksum,
the counts, and for OMOP whether concept IDs were available. When the source is a run
directory, the export is also recorded in that run's manifest.

The output appears only when the export is complete: it is written to ``.partial.<name>`` and
renamed at the end, so a failed or interrupted export never leaves a file that looks finished.
Exporting a run directory takes the run's lock, so it is refused while a batch session is still
writing to that run (the checksum and counts would describe a file that is still growing).

An OMOP export refuses results in which a ``doc_id`` repeats (the batch run counts them as
``duplicate_doc_ids``): both copies would become one ``note_id``, and with ``hash63`` their
identical mentions would share a ``note_nlp_id``. Make the input keys unique and run again.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cuiflow.core.manifests import MANIFEST_NAME, sha256_file, utc_now
from cuiflow.io.writers import format_for, open_writer
from cuiflow.io.writers.jsonl import iter_jsonl

if TYPE_CHECKING:
    from cuiflow.terminology.omop import OmopVocabulary

RESULTS_NAME = "results.jsonl"


def resolve_source(source: Path) -> tuple[Path, Path | None]:
    """``(results file, run directory or None)`` for a run directory or a results file."""
    if source.is_dir():
        results = source / RESULTS_NAME
        if not results.is_file():
            raise FileNotFoundError(f"{results} not found: not a run directory")
        return results, source
    if not source.is_file():
        raise FileNotFoundError(source)
    return source, None


def load_note_ids(path: Path) -> dict[str, int]:
    """A CSV with a header naming ``doc_id`` and ``note_id`` columns."""
    out: dict[str, int] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or not {"doc_id", "note_id"} <= set(reader.fieldnames):
            raise ValueError(f"{path}: needs a header with doc_id and note_id columns")
        for n, row in enumerate(reader, 2):
            try:
                note_id = int(row["note_id"])
            except ValueError:
                raise ValueError(f"{path}:{n}: note_id is not an integer") from None
            if out.setdefault(row["doc_id"], note_id) != note_id:
                raise ValueError(f"{path}:{n}: this doc_id already has a different note_id")
    return out


def note_id_from_doc_id(doc_id: str) -> int | None:
    """Use the document key itself when it is an integer (e.g. a JSONL ``id``); else None.

    ASCII digits only: ``str.isdigit`` also accepts characters such as "²" that ``int`` rejects.
    """
    key = doc_id.strip()
    return int(key) if key.isascii() and key.isdigit() else None


def _run_started(run_dir: Path | None) -> datetime:
    """When the run began: its first session's start, however often it was resumed."""
    if run_dir is not None and (run_dir / MANIFEST_NAME).is_file():
        manifest = json.loads((run_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        sessions = [*manifest.get("previous_sessions", []), manifest]
        started = sessions[0]["started_at"]
        return datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return datetime.now(UTC)


def export_results(
    source: Path,
    out: Path,
    fmt: str | None = None,
    *,
    note_ids: Callable[[str], int | None] | None = None,
    omop_vocab: Path | None = None,
    snippets: bool = False,
    id_scheme: str = "sequential",
    first_id: int = 1,
) -> dict[str, Any]:
    """Stream ``source`` (a run directory or results file) into ``out``; returns the summary.

    Documents the OMOP writer refuses (no ``note_id``) are counted in ``missing_note_id`` and
    left out; nothing else is skipped.
    """
    results, run_dir = resolve_source(source)
    fmt = format_for(out, fmt)
    if out.exists():  # never silently overwrite an earlier export
        raise FileExistsError(f"{out} exists")
    if fmt == "omop":
        if note_ids is None:
            raise ValueError("OMOP output needs note_ids (--note-ids or --note-id-from-doc-id)")
    elif note_ids is not None or omop_vocab is not None or snippets:
        raise ValueError("--note-ids, --omop-vocab and --snippets apply to --format omop only")

    from cuiflow.interfaces.batch import RunLock

    lock = RunLock(run_dir) if run_dir is not None else None
    vocabulary = None
    try:
        options: dict[str, Any] = {}
        if fmt == "omop":
            if omop_vocab is not None:
                from cuiflow.terminology.omop import OmopVocabulary

                vocabulary = OmopVocabulary(omop_vocab)
            options = {
                "note_ids": note_ids,
                "nlp_datetime": _run_started(run_dir),
                "vocabulary": vocabulary,
                "snippets": snippets,
                "id_scheme": id_scheme,
                "first_id": first_id,
            }
        return _export(
            results, run_dir, out, fmt, options, vocabulary, snippets, id_scheme, first_id
        )
    finally:
        if vocabulary is not None:
            vocabulary.close()
        if lock is not None:
            lock.release()


def _export(
    results: Path,
    run_dir: Path | None,
    out: Path,
    fmt: str,
    options: dict[str, Any],
    vocabulary: OmopVocabulary | None,
    snippets: bool,
    id_scheme: str,
    first_id: int,
) -> dict[str, Any]:
    from cuiflow import __version__
    from cuiflow.io.writers.omop import MissingNoteId, OmopNoteNlpWriter

    summary: dict[str, Any] = {
        "cuiflow_version": __version__,
        "exported_at": utc_now(),
        "format": fmt,
        "source": str(results),
        "source_sha256": sha256_file(results),
        "output": str(out),
        "documents": 0,
        "mentions": 0,
    }
    vocab_info = vocabulary.build_info() if vocabulary is not None else None
    # Same suffix as ``out``, so the writer sees the same format (e.g. ``.jsonl.gz``).
    partial = out.with_name(f".partial.{out.name}")
    partial.unlink(missing_ok=True)  # left by an earlier export that was killed
    writer = open_writer(partial, fmt, **options)
    missing = 0
    seen: set[str] | None = set() if fmt == "omop" else None
    try:
        try:
            for n, result in enumerate(iter_jsonl(results), 1):
                if seen is not None:
                    if result.doc_id in seen:
                        raise ValueError(
                            f"{results}: document {n} repeats an earlier doc_id; OMOP needs one "
                            "note per doc_id. Make the input keys unique and run again"
                        )
                    seen.add(result.doc_id)
                try:
                    writer.write(result)
                except MissingNoteId:
                    missing += 1
                    continue
                summary["documents"] += 1
                summary["mentions"] += len(result.mentions)
        finally:
            writer.close()
        partial.replace(out)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    if isinstance(writer, OmopNoteNlpWriter):
        summary.update(
            rows=writer.rows,
            rows_with_concept_id=writer.rows_with_concept,
            missing_note_id=missing,
            omop_vocabulary=vocab_info,
            concept_ids=(
                "from the OMOP vocabulary"
                if vocabulary is not None
                else "0: no OMOP vocabulary given (--omop-vocab)"
            ),
            snippets="off" if not snippets else f"on; {writer.snippets_missing} rows had no text",
            note_nlp_id=(
                "hash63 (needs a bigint column)"
                if id_scheme == "hash63"
                else f"sequential {first_id}..{first_id + writer.rows - 1}"
            ),
        )
    sidecar = out.with_name(out.name + ".export.json")
    sidecar.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if run_dir is not None:
        _record_in_manifest(run_dir, summary)
    return summary


def _record_in_manifest(run_dir: Path, summary: dict[str, Any]) -> None:
    path = run_dir / MANIFEST_NAME
    if not path.is_file():
        return
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.setdefault("exports", []).append(
        {k: v for k, v in summary.items() if k != "omop_vocabulary"}
    )
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    tmp.replace(path)
