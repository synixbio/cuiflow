"""Run manifests (DESIGN_PLAN §6.3).

A manifest records what a run actually did: versions, the effective configuration, the UMLS
release behind each index, input checksums, throughput and an error summary.

**Error records never carry note text.** They hold a document key, the exception type and the
pipeline stage. Exception messages are dropped because tokenizer and parser errors often quote
the input.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANIFEST_NAME = "run_manifest.json"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def git_commit(package_dir: Path | None = None) -> str | None:
    """The commit of the cuiflow source checkout this code runs from, or None.

    Only a ``src/`` layout whose root holds cuiflow's own ``pyproject.toml`` and is the top of
    a git work tree counts. An installed cuiflow inside some other repository (a virtualenv in
    a project directory) must not report that repository's commit as its own.
    """
    pkg = package_dir or Path(__file__).resolve().parents[1]
    root = pkg.parent.parent
    try:
        if pkg.parent.name != "src" or not _CUIFLOW_PROJECT.search(
            (root / "pyproject.toml").read_text(encoding="utf-8")
        ):
            return None
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError):
        return None
    lines = out.stdout.splitlines()
    if out.returncode != 0 or len(lines) != 2:
        return None
    if Path(lines[0]).resolve() != root.resolve():
        return None  # the checkout root is not the top of the work tree: someone else's repo
    return lines[1].strip() or None


_CUIFLOW_PROJECT = re.compile(r'(?m)^name\s*=\s*"cuiflow"\s*$')


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ErrorRecord:
    doc_key: str
    stage: str  # "read", "extract", "codes", "write"
    error_type: str  # exception class name only, never its message

    def to_dict(self) -> dict[str, str]:
        return {"doc_key": self.doc_key, "stage": self.stage, "error_type": self.error_type}


@dataclass
class RunManifest:
    run_id: str
    cuiflow_version: str
    config: dict[str, Any]
    engines: dict[str, dict[str, Any]] = field(default_factory=dict)
    terminology: dict[str, Any] | None = None
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    git_commit: str | None = None
    python: str = field(default_factory=platform.python_version)
    #: ``{"path", "sha256", "size", "mtime_ns"}``. ``sha256`` is null in checkpoints written
    #: before hashing finished: inputs are hashed alongside the run, not before it.
    inputs: list[dict[str, Any]] = field(default_factory=list)
    documents: int = 0
    skipped_resumed: int = 0
    #: Documents whose doc_id an earlier document in this session's inputs already had. They
    #: are still processed, so ``results.jsonl`` holds more than one record with that key.
    duplicate_doc_ids: int = 0
    #: Up to five of those doc_ids, for finding them in the inputs.
    duplicate_doc_id_examples: list[str] = field(default_factory=list)
    #: Text files with bytes that were not valid UTF-8 (replaced by U+FFFD), and up to five of
    #: their doc_ids. Their ``content_sha256`` describes the replaced text, not the file.
    documents_with_invalid_utf8: int = 0
    invalid_utf8_examples: list[str] = field(default_factory=list)
    mentions: int = 0
    errors: list[ErrorRecord] = field(default_factory=list)
    elapsed_seconds: float | None = None
    peak_rss_mb: float | None = None
    #: With worker processes: each worker process's peak RSS, largest first. ``peak_rss_mb`` is
    #: the parent's own; the run's total is at most their sum.
    worker_peak_rss_mb: list[float] | None = None
    #: This session's per-document timeout in seconds; ``None`` when there was none.
    timeout_seconds: float | None = None
    outputs: list[str] = field(default_factory=list)
    #: Documents in the results file when this session finished, across every session.
    documents_in_results: int | None = None
    #: Earlier sessions of a resumed run, oldest first: their counts, timing and errors.
    previous_sessions: list[dict[str, Any]] = field(default_factory=list)
    #: Formats this run's results were exported to (``cuiflow export``).
    exports: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self, max_errors: int | None = None) -> dict[str, Any]:
        """``max_errors`` caps the ``errors`` list (checkpoints; ``errors.jsonl`` has them all).
        ``error_count`` is always the full count."""
        docs_per_sec = (
            round(self.documents / self.elapsed_seconds, 2)
            if self.elapsed_seconds and self.documents
            else None
        )
        return {
            "run_id": self.run_id,
            "cuiflow_version": self.cuiflow_version,
            "git_commit": self.git_commit,
            "python": self.python,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "config": self.config,
            "engines": self.engines,
            "terminology": self.terminology,
            "inputs": self.inputs,
            "documents": self.documents,
            "skipped_resumed": self.skipped_resumed,
            "duplicate_doc_ids": self.duplicate_doc_ids,
            "duplicate_doc_id_examples": self.duplicate_doc_id_examples,
            "documents_with_invalid_utf8": self.documents_with_invalid_utf8,
            "invalid_utf8_examples": self.invalid_utf8_examples,
            "mentions": self.mentions,
            "error_count": len(self.errors),
            "errors": [e.to_dict() for e in self.errors[:max_errors]],
            "errors_truncated": max_errors is not None and len(self.errors) > max_errors,
            "elapsed_seconds": self.elapsed_seconds,
            "docs_per_second": docs_per_sec,
            "peak_rss_mb": self.peak_rss_mb,
            "worker_peak_rss_mb": self.worker_peak_rss_mb,
            "timeout_seconds": self.timeout_seconds,
            "outputs": self.outputs,
            "documents_in_results": self.documents_in_results,
            "previous_sessions": self.previous_sessions,
            "exports": self.exports,
        }

    def write(self, run_dir: Path, *, max_errors: int | None = None) -> Path:
        path = run_dir / MANIFEST_NAME
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(max_errors), indent=2), encoding="utf-8")
        tmp.replace(path)
        return path


def read_manifest(run_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((run_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    return data


def peak_rss_mb() -> float | None:
    """Peak resident memory of this process in MB, where the platform reports it."""
    if sys.platform == "win32":
        return _peak_rss_windows()
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS bytes.
    return round(float(peak) / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def _peak_rss_windows() -> float | None:
    import ctypes
    from ctypes import wintypes

    class _Counters(ctypes.Structure):
        _fields_ = (
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        )

    counters = _Counters()
    counters.cb = ctypes.sizeof(_Counters)
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        # Declare the types: by default ctypes passes the pseudo-handle as a 32-bit int, which
        # the 64-bit call rejects, and the helper silently returned None.
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_Counters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.GetCurrentProcess()
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return round(float(counters.PeakWorkingSetSize) / (1024 * 1024), 1)
    except (AttributeError, OSError):
        pass
    return None
