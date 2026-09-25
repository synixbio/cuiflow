"""Output writers. Every output file is PHI unless shown otherwise (DESIGN_PLAN §10)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from cuiflow.core.models import DocumentResult


class Writer(Protocol):
    path: Path

    def write(self, result: DocumentResult) -> None: ...

    def close(self) -> None: ...


#: Formats :func:`open_writer` accepts, by name.
FORMATS: tuple[str, ...] = ("jsonl", "parquet", "sqlite", "omop")


def format_for(path: Path, fmt: str | None = None) -> str:
    """The output format named by ``fmt``, else implied by the file suffix (default JSONL)."""
    name = (fmt or path.suffix.lstrip(".") or "jsonl").lower()
    return {"gz": "jsonl", "jsonl.gz": "jsonl", "db": "sqlite", "csv": "omop"}.get(name, name)


def open_writer(
    path: Path, fmt: str | None = None, *, append: bool = False, **options: Any
) -> Writer:
    """A writer for ``path``; the format comes from ``fmt`` or the file suffix.

    ``options`` go to the writer: OMOP needs ``note_ids`` and ``nlp_datetime`` (see
    :class:`~cuiflow.io.writers.omop.OmopNoteNlpWriter`). Only JSONL can ``append``.
    """
    fmt = format_for(path, fmt)
    if append and fmt != "jsonl":
        raise ValueError(f"only JSONL output can be appended to, not {fmt}")
    if fmt == "jsonl":
        from cuiflow.io.writers.jsonl import JsonlWriter

        return JsonlWriter(path, append=append)
    if fmt == "parquet":
        from cuiflow.io.writers.parquet import ParquetWriter

        return ParquetWriter(path, **options)
    if fmt == "sqlite":
        from cuiflow.io.writers.sqlite import SqliteWriter

        return SqliteWriter(path, **options)
    if fmt == "omop":
        from cuiflow.io.writers.omop import OmopNoteNlpWriter

        if "note_ids" not in options:
            raise ValueError(
                "OMOP NOTE_NLP output needs each document's note_id: use "
                "cuiflow export --format omop --note-ids <map.csv>"
            )
        return OmopNoteNlpWriter(path, **options)
    raise ValueError(f"unknown output format {fmt!r}")
