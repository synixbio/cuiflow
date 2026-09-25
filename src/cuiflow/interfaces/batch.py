"""Resumable, streaming batch runs (DESIGN_PLAN §6.3, §7.2, §8.1).

Each run writes a new directory that is never overwritten::

    <out_root>/<run_id>/
        results.jsonl        one DocumentResult per line
        run_manifest.json    versions, effective config, inputs, counts, errors
        errors.jsonl         every error record, appended as it happens (no note text)

Workers are processes, not threads (neither engine is thread-safe), and each builds its engines
once in the pool initializer. On Windows processes start with ``spawn``, so nothing
unpicklable (SQLite connections, spaCy models) is ever passed from the parent.

A run resumes with ``resume=<run_dir>``: documents whose key and content hash are already in
``results.jsonl`` are skipped and the rest are appended. Those (key, hash) pairs are indexed in
a temporary SQLite file in the run directory, not in memory, so a resume's memory does not grow
with the number of documents already done.

A text file's key is its path as typed, so a resume must name its inputs as the first session
did (``notes/`` and ``D:/data/notes/`` give different keys, and every document would be done
twice); it refuses otherwise. It also refuses a different configuration, or engines or
terminology whose recorded identity (version, UMLS release, build) has changed. One session at a
time holds a run directory, by an OS lock on ``.run.lock`` that the OS releases however the
session ends, so a killed run can be resumed at once.

A record that cannot be read (a malformed JSONL line, say) is an error record with stage
``read`` and key ``<file>:<line>``, and the run carries on. A worker process that dies (killed
for memory, a crash in native code) takes down the pool: the pool is restarted, the documents
whose results were lost are retried one at a time, and one that kills a worker on its own is an
error record with type ``WorkerDied``.

With ``timeout``, a document that takes longer is an error record with type ``Timeout``. The
clock starts when the document is next in line for a result, so time spent queued behind other
documents does not count, but a new worker's start-up does: set it well above that. The pool's
processes are then terminated and restarted, and the other documents in flight are retried as
for a dead worker. A timeout always runs documents in worker processes, even with one worker.

Input checksums are computed in a background thread while documents are processed, so a large
corpus is not read once just to hash it before the first document runs. A resume reuses the
recorded checksum of a file whose size and modification time are unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Protocol, TypeVar

from cuiflow.api import DocumentInput, Extractor
from cuiflow.core.config import ExtractionConfig
from cuiflow.core.manifests import (
    MANIFEST_NAME,
    ErrorRecord,
    RunManifest,
    git_commit,
    peak_rss_mb,
    sha256_text,
    utc_now,
)
from cuiflow.io.readers import input_files, is_jsonl, iter_documents
from cuiflow.io.writers.jsonl import JsonlWriter

RESULTS_NAME = "results.jsonl"
ERRORS_NAME = "errors.jsonl"
#: Checkpoints list at most this many errors, so their cost does not grow with a run's errors;
#: the final manifest and ``errors.jsonl`` list them all.
CHECKPOINT_MAX_ERRORS = 100
#: Rewrite the manifest after this many documents, so a killed run's counts survive.
CHECKPOINT_EVERY = 1_000

T = TypeVar("T")
R = TypeVar("R")
#: ("ok", result dict, rss) or ("error", error record dict, rss); see ``_process_one``.
Outcome = tuple[str, Any, tuple[int, float | None]]

# ---- worker side ------------------------------------------------------------------------------

_worker_extractor: Extractor | None = None


def _init_worker(config: ExtractionConfig) -> None:
    global _worker_extractor
    _worker_extractor = Extractor(config)


def _process_one(doc: DocumentInput) -> Outcome:
    """Returns ("ok", result_dict, rss) or ("error", error_record_dict, rss). Never raises.

    ``rss`` is (worker pid, the worker's peak RSS in MB so far), so the parent can report each
    worker's memory."""
    assert _worker_extractor is not None, "worker not initialized"
    try:
        result = _worker_extractor.process(doc.text, doc.doc_id)
    except Exception as exc:  # isolate per document; record the type only (no PHI)
        error = ErrorRecord(doc.doc_id, "extract", type(exc).__name__).to_dict()
        return "error", error, (os.getpid(), peak_rss_mb())
    return "ok", result.to_dict(), (os.getpid(), peak_rss_mb())


def _worker_info() -> dict[str, Any]:
    assert _worker_extractor is not None, "worker not initialized"
    return _worker_extractor.info()


# ---- parent side ------------------------------------------------------------------------------


class WorkerPool:
    """A process pool that is replaced when a worker dies."""

    def __init__(self, config: ExtractionConfig, workers: int) -> None:
        self._config = config
        self._workers = workers
        self._pool = self._start()

    def _start(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(
            max_workers=self._workers, initializer=_init_worker, initargs=(self._config,)
        )

    def submit(self, fn: Callable[..., R], *args: Any) -> Future[R]:
        return self._pool.submit(fn, *args)

    def kill(self) -> None:
        """Terminate every worker at once, as for a stuck document; :meth:`restart` follows."""
        terminate = getattr(self._pool, "terminate_workers", None)  # Python 3.14+
        if terminate is not None:
            terminate()
            return
        for process in list((self._pool._processes or {}).values()):
            process.terminate()

    def restart(self) -> None:
        """Replace a broken pool, and check that the new one's workers start."""
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._pool = self._start()
        try:
            self._pool.submit(_worker_info).result()
        except BrokenProcessPool:
            raise RuntimeError("worker processes cannot start after a worker died") from None

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)


class _Pool(Protocol):
    def submit(self, fn: Callable[..., R], *args: Any) -> Future[R]: ...
    def kill(self) -> None: ...
    def restart(self) -> None: ...


def _pooled(
    pool: _Pool, docs: Iterable[DocumentInput], in_flight: int, timeout: float | None = None
) -> Iterator[Outcome]:
    """Every document's outcome, in input order, never more than ``in_flight`` pending.

    When a worker dies, the documents whose results were lost are retried one at a time in a
    fresh pool. One that breaks the pool on its own is recorded as ``WorkerDied``; the others
    are not blamed for it. With ``timeout``, the document next in line gets that many seconds
    from when it became next; one that overruns is recorded as ``Timeout``, the workers are
    killed, and the rest are retried in the same way.
    """
    pending: deque[tuple[DocumentInput, Future[Outcome]]] = deque()
    items = iter(docs)
    exhausted = False
    deadline: float | None = None
    while True:
        while not exhausted and len(pending) < in_flight:
            doc = next(items, None)
            if doc is None:
                exhausted = True
            else:
                pending.append((doc, _submit(pool, doc)))
        if not pending:
            return
        if timeout is not None and deadline is None:
            deadline = time.monotonic() + timeout
        try:
            wait = None if deadline is None else max(deadline - time.monotonic(), 0.0)
            outcome = pending[0][1].result(timeout=wait)
        except (BrokenProcessPool, TimeoutError) as exc:
            lost = list(pending)
            pending.clear()
            deadline = None
            if isinstance(exc, TimeoutError):
                pool.kill()
                pool.restart()
                yield _failed(lost.pop(0)[0], "Timeout")
            else:
                pool.restart()
            for d, f in lost:
                if f.done() and not f.cancelled() and f.exception() is None:
                    yield f.result()
                else:
                    yield _alone(pool, d, timeout)
            continue
        pending.popleft()
        deadline = None
        yield outcome


def _submit(pool: _Pool, doc: DocumentInput) -> Future[Outcome]:
    # A broken pool refuses new work at once; fail the future so it is handled in order.
    try:
        return pool.submit(_process_one, doc)
    except BrokenProcessPool as exc:
        failed: Future[Outcome] = Future()
        failed.set_exception(exc)
        return failed


def _alone(pool: _Pool, doc: DocumentInput, timeout: float | None) -> Outcome:
    """``doc``'s outcome with nothing else in flight: ``WorkerDied`` if it breaks the pool,
    ``Timeout`` if it overruns."""
    try:
        return _submit(pool, doc).result(timeout=timeout)
    except BrokenProcessPool:
        pool.restart()
        return _failed(doc, "WorkerDied")
    except TimeoutError:
        pool.kill()
        pool.restart()
        return _failed(doc, "Timeout")


def _failed(doc: DocumentInput, error_type: str) -> Outcome:
    return "error", ErrorRecord(doc.doc_id, "extract", error_type).to_dict(), (0, None)


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def _truncate_torn_tail(results: Path) -> int:
    """Cut a partial last line left by a killed run; returns the bytes removed.

    Appending after a torn line would glue the next document onto it and corrupt both. Every
    complete line ends with ``\\n`` (the writer flushes whole lines), so everything after the
    last newline is the torn part; that document is redone.
    """
    size = results.stat().st_size
    if size == 0:
        return 0
    with results.open("rb+") as fh:
        pos = size
        while pos > 0:
            step = min(1 << 16, pos)
            fh.seek(pos - step)
            chunk = fh.read(step)
            nl = chunk.rfind(b"\n")
            if nl >= 0:
                keep = pos - step + nl + 1
                break
            pos -= step
        else:
            keep = 0
        if keep < size:
            fh.truncate(keep)
        return size - keep


DONE_INDEX_NAME = ".resume-done.sqlite"

_DECODER = json.JSONDecoder()
_DOC_ID_KEY = '"doc_id": '
_SHA_KEY = '"content_sha256": "'


def _done_key(line: str) -> tuple[str, str]:
    """A result line's (doc_id, content hash), without parsing its mentions.

    The writer puts ``doc_id`` second and ``metadata`` after the mentions. Inside a JSON string
    every quote is escaped, so an unescaped ``"content_sha256": "`` can only be a key. A line
    that does not fit is parsed in full.
    """
    at = line.find(_DOC_ID_KEY, 0, 200)
    sha_at = line.rfind(_SHA_KEY)
    if at != -1 and sha_at != -1:
        try:
            doc_id, _ = _DECODER.raw_decode(line, at + len(_DOC_ID_KEY))
        except ValueError:
            doc_id = None
        sha_at += len(_SHA_KEY)
        sha = line[sha_at : sha_at + 64]
        if isinstance(doc_id, str) and line[sha_at + 64 : sha_at + 65] == '"':
            return doc_id, sha
    d = json.loads(line)
    return d["doc_id"], d.get("metadata", {}).get("content_sha256", "")


class DoneKeys:
    """The (doc_id, content hash) pairs already in ``results.jsonl``, indexed on disk.

    A set of tuples costs about 150 bytes a document (1.5 GB at ten million); this costs a
    small page cache. The index is rebuilt from ``results.jsonl`` on every resume, so a stale
    one left by a killed session is never trusted."""

    def __init__(self, results: Path) -> None:
        self.path = results.parent / DONE_INDEX_NAME
        self.path.unlink(missing_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.executescript(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
            "CREATE TABLE done (doc_id TEXT, sha TEXT, PRIMARY KEY (doc_id, sha)) WITHOUT ROWID;"
            "CREATE TABLE seen (doc_id TEXT PRIMARY KEY) WITHOUT ROWID;"
        )
        self._n = 0
        #: Result lines, which a repeated input makes more than the distinct keys.
        self.lines = 0
        if results.is_file():
            with results.open(encoding="utf-8") as fh:
                self._db.executemany("INSERT OR IGNORE INTO done VALUES (?, ?)", self._keys(fh))
            self._db.commit()
            (self._n,) = self._db.execute("SELECT count(*) FROM done").fetchone()

    def _keys(self, lines: Iterable[str]) -> Iterator[tuple[str, str]]:
        for line in lines:
            if line.strip():
                self.lines += 1
                yield _done_key(line)

    def __contains__(self, key: object) -> bool:
        if not self._n or not isinstance(key, tuple):
            return False
        return (
            self._db.execute("SELECT 1 FROM done WHERE doc_id = ? AND sha = ?", key).fetchone()
            is not None
        )

    def __len__(self) -> int:
        return self._n

    def first_sight(self, doc_id: str) -> bool:
        """False when this session's inputs already had a document with ``doc_id``."""
        return self._db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (doc_id,)).rowcount == 1

    def close(self) -> None:
        self._db.close()
        self.path.unlink(missing_ok=True)


LOCK_NAME = ".run.lock"


class RunInUse(ValueError):
    """Another session holds the run directory."""


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

else:
    import fcntl

    def _try_lock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class RunLock:
    """An exclusive lock on a run directory, held by one session.

    It is an OS file lock, not a marker file: the OS releases it when the holder exits, even by
    a hard kill, so a stale lock never blocks a resume. The lock file itself stays."""

    def __init__(self, run_dir: Path) -> None:
        self._fh: IO[bytes] = (run_dir / LOCK_NAME).open("a+b")
        try:
            _try_lock(self._fh)
        except OSError:
            self._fh.close()
            raise RunInUse(f"{run_dir} is in use by another cuiflow session") from None

    def release(self) -> None:
        self._fh.close()  # closing the handle releases the lock


def _check_inputs(run_dir: Path, previous: dict[str, Any], inputs: list[str | Path]) -> None:
    """Refuses a resume that does not name the earlier text inputs as before (they are keys).

    JSONL inputs (``.jsonl``, ``.jsonl.gz``) are exempt: their keys come from the records."""
    now = {str(p) for p in input_files(inputs)}
    missing = [
        p["path"]
        for p in previous.get("inputs", [])
        if not is_jsonl(Path(p["path"])) and p["path"] not in now
    ]
    if missing:
        raise ValueError(
            f"cannot resume {run_dir}: {len(missing)} of its input files are not among these "
            f"inputs as named before, e.g. {missing[0]!r}. Document keys are paths as typed: "
            "resume from the same directory with the same input paths (see run_manifest.json), "
            "or start a new run"
        )


def _check_identity(run_dir: Path, previous: dict[str, Any], info: dict[str, Any]) -> None:
    """Refuses to add results from other engines or terminology to an earlier session's."""
    engine = previous.get("engines", {}).get("engine")
    if engine is None:
        return  # no earlier session got as far as recording its engines
    changed = [
        name
        for name, before in (("engine", engine), ("terminology", previous.get("terminology")))
        if before != info[name]
    ]
    if changed:
        raise ValueError(
            f"cannot resume {run_dir}: its {' and '.join(changed)} differ from this session's "
            "(another version, or an index rebuilt at the same path); start a new run"
        )


def _previous_manifest(run_dir: Path, config: ExtractionConfig) -> dict[str, Any]:
    """The interrupted run's manifest; refuses a resume under different settings (§6.3)."""
    path = run_dir / MANIFEST_NAME
    if not path.is_file():
        return {}
    previous: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if previous.get("config") != config.to_dict():
        raise ValueError(
            f"cannot resume {run_dir}: its configuration differs from this one "
            "(start a new run, or resume with the settings in its run_manifest.json)"
        )
    return previous


_SESSION_KEYS = (
    "started_at",
    "finished_at",
    "cuiflow_version",
    "git_commit",
    "documents",
    "skipped_resumed",
    "duplicate_doc_ids",
    "documents_with_invalid_utf8",
    "mentions",
    "error_count",
    "errors",
    "elapsed_seconds",
    "peak_rss_mb",
    "worker_peak_rss_mb",
    "timeout_seconds",
)


def _session_of(manifest: dict[str, Any]) -> dict[str, Any]:
    """One earlier session's own figures. ``finished_at`` is null for a session that was killed."""
    return {k: manifest.get(k) for k in _SESSION_KEYS}


def run_batch(
    inputs: Iterable[str | Path],
    out_root: Path,
    config: ExtractionConfig,
    *,
    workers: int = 1,
    run_id: str | None = None,
    resume: Path | None = None,
    progress: Callable[[int], None] | None = None,
    timeout: float | None = None,
) -> Path:
    """Process every input document into a run directory; returns that directory.

    ``timeout`` is seconds per document (see the module docstring); ``None`` waits forever.
    """
    if timeout is not None and not timeout > 0:
        raise ValueError("timeout must be a positive number of seconds")
    inputs = list(inputs)
    if resume is not None:
        run_dir = resume
        if not (run_dir / RESULTS_NAME).is_file():
            raise FileNotFoundError(f"nothing to resume: {run_dir / RESULTS_NAME} not found")
    else:
        run_dir = out_root / (run_id or new_run_id())
        run_dir.mkdir(parents=True, exist_ok=False)  # fails rather than overwrite a run
    lock = RunLock(run_dir)
    try:
        _run(run_dir, inputs, config, workers, resume is not None, progress, timeout)
    finally:
        lock.release()
    return run_dir


def _run(
    run_dir: Path,
    inputs: list[str | Path],
    config: ExtractionConfig,
    workers: int,
    resuming: bool,
    progress: Callable[[int], None] | None,
    timeout: float | None,
) -> None:
    from cuiflow import __version__

    previous: dict[str, Any] = {}
    if resuming:
        previous = _previous_manifest(run_dir, config)
        _check_inputs(run_dir, previous, inputs)
        _truncate_torn_tail(run_dir / RESULTS_NAME)
    results_path = run_dir / RESULTS_NAME
    done = DoneKeys(results_path)
    files = input_files(inputs)
    stop_hashing = threading.Event()
    hasher = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cuiflow-hash")
    hashed = hasher.submit(_hash_inputs, files, previous.get("inputs", []), stop_hashing)

    manifest = RunManifest(
        run_id=run_dir.name,
        cuiflow_version=__version__,
        config=config.to_dict(),
        git_commit=git_commit(),
        inputs=[{"path": str(p), "sha256": None} for p in files],
        outputs=[RESULTS_NAME],
        previous_sessions=[
            *previous.get("previous_sessions", []),
            *([_session_of(previous)] if previous else []),
        ],
        exports=previous.get("exports", []),
        timeout_seconds=timeout,
    )
    if not previous.get("engines"):
        manifest.write(run_dir)  # nothing recorded to check against, so write it at once

    def begin(info: dict[str, Any]) -> None:
        # Checked once the engines are up, and before this session touches the manifest.
        _check_identity(run_dir, previous, info)
        manifest.engines = {"engine": info["engine"]}
        manifest.terminology = info["terminology"]
        manifest.write(run_dir)

    def todo() -> Iterator[DocumentInput]:
        def unreadable(location: str, error_type: str) -> None:
            record_error(ErrorRecord(location, "read", error_type))

        for doc in iter_documents(inputs, on_error=unreadable):
            if not done.first_sight(doc.doc_id):
                # Kept, not dropped: which copy is right is the user's call. Counted so that
                # the doc_id is not mistaken for a unique key downstream.
                manifest.duplicate_doc_ids += 1
                if len(manifest.duplicate_doc_id_examples) < 5:
                    manifest.duplicate_doc_id_examples.append(doc.doc_id)
            if doc.replaced:
                manifest.documents_with_invalid_utf8 += 1
                if len(manifest.invalid_utf8_examples) < 5:
                    manifest.invalid_utf8_examples.append(doc.doc_id)
            if (doc.doc_id, sha256_text(doc.text)) in done:
                manifest.skipped_resumed += 1
                continue
            yield doc

    worker_rss: dict[int, float | None] = {}
    started = time.perf_counter()
    writer = JsonlWriter(results_path, append=True)
    errors_fh = (run_dir / ERRORS_NAME).open("a", encoding="utf-8", newline="\n")

    def record_error(error: ErrorRecord) -> None:
        manifest.errors.append(error)
        errors_fh.write(json.dumps({**error.to_dict(), "session": manifest.started_at}) + "\n")
        errors_fh.flush()

    try:
        if workers <= 1 and timeout is None:
            _init_worker(config)
            info = _worker_info()
            begin(info)
            outcomes: Iterator[Outcome] = (_process_one(d) for d in todo())
            _consume(outcomes, writer, record_error, manifest, run_dir, progress, worker_rss)
        else:
            pool = WorkerPool(config, max(workers, 1))
            try:
                info = pool.submit(_worker_info).result()
                begin(info)
                outcomes = _pooled(pool, todo(), in_flight=max(workers, 1) * 4, timeout=timeout)
                _consume(outcomes, writer, record_error, manifest, run_dir, progress, worker_rss)
            finally:
                pool.shutdown()
        manifest.inputs = hashed.result()
    finally:
        stop_hashing.set()  # an interrupted run does not wait for a multi-GB hash
        hasher.shutdown(wait=True)
        writer.close()
        errors_fh.close()
        done.close()
        global _worker_extractor
        if _worker_extractor is not None:
            _worker_extractor.close()
            _worker_extractor = None

    manifest.documents_in_results = done.lines + manifest.documents
    manifest.finished_at = utc_now()
    manifest.elapsed_seconds = round(time.perf_counter() - started, 3)
    manifest.peak_rss_mb = peak_rss_mb()
    if worker_rss:
        manifest.worker_peak_rss_mb = sorted(
            (v for v in worker_rss.values() if v is not None), reverse=True
        )
    manifest.write(run_dir)


def _hash_inputs(
    files: list[Path], previous: list[dict[str, Any]], stop: threading.Event
) -> list[dict[str, Any]]:
    """Each input's checksum, reusing an earlier session's when size and mtime are unchanged."""
    known = {p["path"]: p for p in previous if p.get("sha256") and "mtime_ns" in p}
    out: list[dict[str, Any]] = []
    for path in files:
        try:
            st = path.stat()
            prev = known.get(str(path))
            if prev and (prev["size"], prev["mtime_ns"]) == (st.st_size, st.st_mtime_ns):
                sha = prev["sha256"]
            else:
                h = hashlib.sha256()
                with path.open("rb") as fh:
                    while block := fh.read(1 << 20):
                        if stop.is_set():
                            raise InterruptedError("input hashing stopped")
                        h.update(block)
                sha = h.hexdigest()
        except InterruptedError:
            raise
        except OSError as exc:  # gone or unreadable since it was read: the run itself stands
            out.append({"path": str(path), "sha256": None, "error": type(exc).__name__})
            continue
        out.append(
            {"path": str(path), "sha256": sha, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        )
    return out


def _consume(
    outcomes: Iterable[Outcome],
    writer: JsonlWriter,
    record_error: Callable[[ErrorRecord], None],
    manifest: RunManifest,
    run_dir: Path,
    progress: Callable[[int], None] | None,
    worker_rss: dict[int, float | None],
) -> None:
    from cuiflow.core.models import DocumentResult

    for n, (status, payload, (pid, rss)) in enumerate(outcomes, 1):
        worker_rss[pid] = rss
        if status == "ok":
            writer.write(DocumentResult.from_dict(payload))
            manifest.documents += 1
            manifest.mentions += len(payload["mentions"])
        else:
            record_error(ErrorRecord(**payload))
        if progress:
            progress(manifest.documents + len(manifest.errors))
        if n % CHECKPOINT_EVERY == 0:
            # A killed session still shows how far it got; errors.jsonl has every error.
            manifest.write(run_dir, max_errors=CHECKPOINT_MAX_ERRORS)
