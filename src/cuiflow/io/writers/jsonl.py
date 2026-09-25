"""One ``DocumentResult`` per line; gzip when the path ends in ``.gz``."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from cuiflow.core.models import DocumentResult


class JsonlWriter:
    def __init__(self, path: Path, *, append: bool = False) -> None:
        self.path = path
        # "x", not "w": never silently overwrite a previous run's output. The writer owns its
        # handle for its whole lifetime, hence no context manager.
        self._fh: IO[str]
        if path.suffix.lower() == ".gz":
            self._fh = gzip.open(path, "at" if append else "xt", encoding="utf-8")  # noqa: SIM115
        else:
            self._fh = path.open("a" if append else "x", encoding="utf-8", newline="\n")

    def write(self, result: DocumentResult) -> None:
        self._fh.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def iter_jsonl(path: Path) -> Iterator[DocumentResult]:
    """Stream results back one document at a time (exports, resuming, tooling).

    A torn final line, left by a run that was killed mid-write, is skipped: resuming redoes
    that document.
    """
    opener = gzip.open if path.suffix.lower() == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            if not line.endswith("\n"):
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    return
            else:
                data = json.loads(line)
            yield DocumentResult.from_dict(data)


def read_jsonl(path: Path) -> list[DocumentResult]:
    """Read results back into memory (tests, small files). Prefer :func:`iter_jsonl`."""
    return list(iter_jsonl(path))
