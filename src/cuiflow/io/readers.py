"""Read documents from files, folders, JSONL and stdin.

Text files are read with line endings untranslated, as mmlite does, so character offsets index
the file verbatim. A leading UTF-8 byte-order mark is dropped (it is not part of the note, and
left in, it hides the first heading from section detection). Bytes that are not valid UTF-8
become U+FFFD and are counted in ``DocumentInput.replaced``; batch runs record them. JSONL files
are decoded the same way, line by line.

A record that cannot be read (a malformed JSONL line, a record with no string ``text``, a file
that cannot be opened) is reported with where it is, ``<file>:<line>`` or the file's path, and
the exception type: never its content. By default that stops the read (:class:`InputError`);
pass ``on_error`` to record it and carry on, as the CLI and batch runner do.

Every document's key is its source path (or JSONL ``id``). **Keys are often patient
identifiers**: they are PHI, like the text.

Other formats (BioC, PubMed, …) are planned via mmlite's own loaders (DESIGN_PLAN §2.1).
"""

from __future__ import annotations

import codecs
import gzip
import json
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from cuiflow.api import DocumentInput

TEXT_SUFFIXES = frozenset({".txt", ".text"})

#: ``on_error(location, error_type)``: a record or file that could not be read.
OnError = Callable[[str, str], None]


class InputError(ValueError):
    """An input record or file that cannot be read. The message names where, never what."""

    def __init__(self, location: str, error_type: str) -> None:
        super().__init__(f"cannot read {location}: {error_type}")
        self.location = location
        self.error_type = error_type


_BOM = codecs.BOM_UTF8
_REPLACEMENT = chr(0xFFFD)
_counts = threading.local()


def _count_and_replace(exc: UnicodeError) -> tuple[str, int]:
    _counts.n += 1
    return _REPLACEMENT, exc.end  # type: ignore[attr-defined]


codecs.register_error("cuiflow.count-replace", _count_and_replace)


def decode_utf8(data: bytes) -> tuple[str, int]:
    """``data`` decoded as ``errors="replace"`` would, and how many replacements were made.

    A leading BOM is dropped."""
    if data.startswith(_BOM):
        data = data[len(_BOM) :]
    _counts.n = 0
    text = data.decode("utf-8", errors="cuiflow.count-replace")
    return text, _counts.n


def _read_text(path: Path) -> DocumentInput:
    text, replaced = decode_utf8(path.read_bytes())
    return DocumentInput(doc_id=str(path), text=text, replaced=replaced)


def is_jsonl(path: Path) -> bool:
    return path.name.lower().endswith((".jsonl", ".jsonl.gz"))


def _jsonl_record(path: Path, n: int, line: bytes) -> DocumentInput:
    text, replaced = decode_utf8(line)
    record = json.loads(text)
    if not isinstance(record, dict):
        raise TypeError("record is not a JSON object")
    body = record.get("text")
    if not isinstance(body, str):
        raise KeyError("text")
    # A missing or empty id falls through; 0 is an id.
    doc_id = next(
        (v for v in (record.get("id"), record.get("doc_id")) if v is not None and v != ""),
        f"{path.name}:{n}",
    )
    return DocumentInput(doc_id=str(doc_id), text=body, replaced=replaced)


def _iter_jsonl(path: Path, on_error: OnError) -> Iterator[DocumentInput]:
    gzipped = path.name.lower().endswith(".gz")
    try:
        fh = gzip.open(path, "rb") if gzipped else path.open("rb")  # noqa: SIM115 (closed below)
    except OSError as exc:
        on_error(str(path), type(exc).__name__)
        return
    with fh:
        n = 0
        try:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    doc = _jsonl_record(path, n, line)
                except (ValueError, TypeError, KeyError) as exc:
                    on_error(f"{path}:{n}", type(exc).__name__)
                    continue
                yield doc
        except (OSError, EOFError) as exc:  # a truncated .gz, a file gone mid-read
            on_error(f"{path}:{n + 1}", type(exc).__name__)


def _raise(location: str, error_type: str) -> None:
    raise InputError(location, error_type)


def _in_folder(folder: Path) -> list[Path]:
    """The text files a folder contributes. JSONL files in a folder are not read: an output
    written next to the notes would be read back as input. Name a JSONL file directly."""
    return [
        c for c in sorted(folder.rglob("*")) if c.is_file() and c.suffix.lower() in TEXT_SUFFIXES
    ]


def skipped_in_folders(paths: Iterable[str | Path]) -> list[Path]:
    """Files inside the folders among ``paths`` that are not read (not ``.txt`` / ``.text``)."""
    out: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if str(raw) != "-" and path.is_dir():
            out.extend(
                c
                for c in sorted(path.rglob("*"))
                if c.is_file() and c.suffix.lower() not in TEXT_SUFFIXES
            )
    return out


def iter_documents(
    paths: Iterable[str | Path], *, on_error: OnError | None = None
) -> Iterator[DocumentInput]:
    """Documents from each path: a text file, a ``.jsonl`` or ``.jsonl.gz`` file, a folder of
    text files, or ``-``.

    Folders are walked recursively in sorted order so runs are reproducible; they contribute
    ``.txt`` and ``.text`` files only (:func:`skipped_in_folders` lists the rest). A path that does
    not exist raises ``FileNotFoundError``. A record or file that cannot be read goes to
    ``on_error``, or raises :class:`InputError` without one.
    """
    report = on_error or _raise
    for raw in paths:
        if str(raw) == "-":
            yield DocumentInput(doc_id="stdin", text=sys.stdin.read())
            continue
        path = Path(raw)
        if path.is_dir():
            files = _in_folder(path)
        elif path.is_file():
            files = [path]
        else:
            raise FileNotFoundError(path)
        for f in files:
            if is_jsonl(f):
                yield from _iter_jsonl(f, report)
                continue
            try:
                doc = _read_text(f)
            except OSError as exc:
                report(str(f), type(exc).__name__)
                continue
            yield doc


def input_files(paths: Iterable[str | Path]) -> list[Path]:
    """The files ``iter_documents`` would read, for manifest checksums."""
    out: list[Path] = []
    for raw in paths:
        if str(raw) == "-":
            continue
        path = Path(raw)
        if path.is_dir():
            out.extend(_in_folder(path))
        elif path.is_file():
            out.append(path)
    return out
