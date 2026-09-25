from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

import pytest

from cuiflow.api import DocumentInput
from cuiflow.core.config import ExtractionConfig
from cuiflow.interfaces.batch import RESULTS_NAME, _pooled, run_batch
from cuiflow.io.writers.jsonl import read_jsonl


def _notes(root: Path, texts: dict[str, str]) -> Path:
    root.mkdir()
    for name, text in texts.items():
        (root / name).write_text(text, encoding="utf-8")
    return root


def test_run_directory_and_manifest(tmp_path: Path, patch_extractor: Callable[..., None]) -> None:
    patch_extractor()
    notes = _notes(tmp_path / "notes", {"a.txt": "No chest pain.", "b.txt": "Diabetes."})
    run_dir = run_batch([notes], tmp_path / "runs", ExtractionConfig(), run_id="r1")

    results = read_jsonl(run_dir / RESULTS_NAME)
    assert [Path(r.doc_id).name for r in results] == ["a.txt", "b.txt"]
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["documents"] == 2 and manifest["errors"] == []
    assert manifest["config"]["mode"] == "single:umlsmatch"
    assert len(manifest["inputs"]) == 2 and all(len(i["sha256"]) == 64 for i in manifest["inputs"])
    assert manifest["finished_at"] is not None

    with pytest.raises(FileExistsError):  # never overwrite a run
        run_batch([notes], tmp_path / "runs", ExtractionConfig(), run_id="r1")


def test_errors_are_isolated_and_carry_no_text(
    tmp_path: Path, patch_extractor: Callable[..., None]
) -> None:
    patch_extractor(fail_on="SECRET")
    notes = _notes(tmp_path / "notes", {"a.txt": "chest pain", "b.txt": "SECRET patient text"})
    run_dir = run_batch([notes], tmp_path / "runs", ExtractionConfig())
    raw = (run_dir / "run_manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(raw)
    assert manifest["documents"] == 1
    assert manifest["errors"] == [
        {"doc_key": str(notes / "b.txt"), "stage": "extract", "error_type": "RuntimeError"}
    ]
    assert "SECRET" not in raw  # the exception message quoted the note; it must not leak


def test_resume_skips_done_documents(tmp_path: Path, patch_extractor: Callable[..., None]) -> None:
    patch_extractor()
    notes = _notes(tmp_path / "notes", {"a.txt": "chest pain"})
    run_dir = run_batch([notes], tmp_path / "runs", ExtractionConfig())
    (notes / "b.txt").write_text("metformin", encoding="utf-8")

    run_batch([notes], tmp_path / "runs", ExtractionConfig(), resume=run_dir)
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["skipped_resumed"] == 1 and manifest["documents"] == 1
    assert len(read_jsonl(run_dir / RESULTS_NAME)) == 2


def test_duplicate_doc_ids_are_kept_and_counted(
    tmp_path: Path, patch_extractor: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    import sqlite3

    from cuiflow.interfaces.cli import main
    from cuiflow.interfaces.export import export_results

    patch_extractor()
    notes = tmp_path / "notes.jsonl"
    records = [("1", "chest pain"), ("2", "metformin"), ("1", "No chest pain."), ("1", "x")]
    notes.write_text(
        "".join(json.dumps({"id": i, "text": t}) + "\n" for i, t in records), encoding="utf-8"
    )
    assert main(["extract", str(notes), "--out-dir", str(tmp_path / "runs"), "--run-id", "r"]) == 0
    assert "2 documents reuse an earlier doc_id (e.g. 1, 1)" in capsys.readouterr().err
    run_dir = tmp_path / "runs" / "r"
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["duplicate_doc_ids"] == 2 and manifest["documents"] == 4
    # Every copy survives an export to SQLite, whose document key is no longer UNIQUE.
    export_results(run_dir, tmp_path / "m.sqlite")
    rows = sqlite3.connect(tmp_path / "m.sqlite").execute("SELECT source FROM documents")
    assert sorted(r[0] for r in rows) == ["1", "1", "1", "2"]


def test_documents_in_results_counts_lines_not_keys(
    tmp_path: Path, patch_extractor: Callable[..., None]
) -> None:
    patch_extractor()
    notes = tmp_path / "notes.jsonl"
    record = json.dumps({"id": "1", "text": "chest pain"}) + "\n"
    notes.write_text(record * 2, encoding="utf-8")  # the same record twice
    run_dir = run_batch([notes], tmp_path / "runs", ExtractionConfig())
    run_batch([notes], tmp_path / "runs", ExtractionConfig(), resume=run_dir)
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert len(read_jsonl(run_dir / RESULTS_NAME)) == manifest["documents_in_results"] == 2


def test_every_error_goes_to_errors_jsonl_and_checkpoints_are_capped(
    tmp_path: Path, patch_extractor: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    import cuiflow.interfaces.batch as batch
    from cuiflow.core.manifests import RunManifest

    patch_extractor(fail_on="SECRET")
    monkeypatch.setattr(batch, "CHECKPOINT_EVERY", 1)
    monkeypatch.setattr(batch, "CHECKPOINT_MAX_ERRORS", 1)
    checkpoints: list[dict[str, object]] = []
    real_write = RunManifest.write

    def write(self: RunManifest, run_dir: Path, *, max_errors: int | None = None) -> None:
        real_write(self, run_dir, max_errors=max_errors)
        if max_errors is not None:
            checkpoints.append(json.loads((run_dir / "run_manifest.json").read_text("utf-8")))

    monkeypatch.setattr(RunManifest, "write", write)
    texts = {f"{n}.txt": "SECRET" for n in range(3)}
    run_dir = run_batch([_notes(tmp_path / "notes", texts)], tmp_path / "runs", ExtractionConfig())

    last = checkpoints[-1]
    assert last["error_count"] == 3 and len(last["errors"]) == 1  # type: ignore[arg-type]
    assert last["errors_truncated"] is True
    lines = (run_dir / batch.ERRORS_NAME).read_text(encoding="utf-8").splitlines()
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert len(lines) == 3 and manifest["error_count"] == 3 and len(manifest["errors"]) == 3
    assert {json.loads(line)["session"] for line in lines} == {manifest["started_at"]}
    assert "SECRET" not in "".join(lines)


def test_resume_reuses_checksums_of_unchanged_inputs(
    tmp_path: Path, patch_extractor: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    patch_extractor()
    notes = _notes(tmp_path / "notes", {"a.txt": "chest pain", "b.txt": "metformin"})
    run_dir = run_batch([notes], tmp_path / "runs", ExtractionConfig())
    first = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))["inputs"]
    assert all(len(i["sha256"]) == 64 and "mtime_ns" in i for i in first)

    (notes / "b.txt").write_text("metformin 500 mg", encoding="utf-8")
    hashed: list[str] = []
    real_open = Path.open

    def spy(self: Path, *args: object, **kw: object):  # type: ignore[no-untyped-def]
        if args and args[0] == "rb" and threading.current_thread().name.startswith("cuiflow-hash"):
            hashed.append(self.name)
        return real_open(self, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", spy)
    run_batch([notes], tmp_path / "runs", ExtractionConfig(), resume=run_dir)
    assert hashed == ["b.txt"]  # a.txt is unchanged: its checksum is carried over
    second = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))["inputs"]
    assert second[0]["sha256"] == first[0]["sha256"] != second[1]["sha256"]


def test_hashing_stops_when_asked(tmp_path: Path) -> None:
    import threading

    from cuiflow.interfaces.batch import _hash_inputs

    f = tmp_path / "big.txt"
    f.write_bytes(b"x" * 10)
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        _hash_inputs([f], [], stop)
    missing = _hash_inputs([tmp_path / "gone.txt"], [], threading.Event())
    assert missing == [
        {"path": str(tmp_path / "gone.txt"), "sha256": None, "error": "FileNotFoundError"}
    ]


def test_unreadable_records_are_errors_and_the_run_carries_on(
    tmp_path: Path, patch_extractor: Callable[..., None]
) -> None:
    patch_extractor()
    j = tmp_path / "in.jsonl"
    j.write_text(
        '{"id": "a", "text": "chest pain"}\n'
        '{"id": "b", "text": "SECRET\n'  # truncated
        '{"id": "c", "text": "x"}\n',
        encoding="utf-8",
    )
    run_dir = run_batch([j], tmp_path / "runs", ExtractionConfig())
    assert [r.doc_id for r in read_jsonl(run_dir / RESULTS_NAME)] == ["a", "c"]
    raw = (run_dir / "run_manifest.json").read_text(encoding="utf-8")
    assert json.loads(raw)["errors"] == [
        {"doc_key": f"{j}:2", "stage": "read", "error_type": "JSONDecodeError"}
    ]
    assert "SECRET" not in raw + (run_dir / "errors.jsonl").read_text(encoding="utf-8")


class _BreakingPool:
    """Runs work inline. Document "kill" breaks the pool, like a worker that dies: its future
    fails, and so does all work submitted until a restart."""

    def __init__(self) -> None:
        self.broken = False
        self.restarts = 0

    def submit(self, fn: Callable[..., Any], doc: DocumentInput) -> Future[Any]:
        self.broken = self.broken or doc.doc_id == "kill"
        if self.broken and doc.doc_id != "kill":
            raise BrokenProcessPool("a worker died")  # as ProcessPoolExecutor.submit does
        future: Future[Any] = Future()
        if self.broken:
            future.set_exception(BrokenProcessPool("a worker died"))
        else:
            future.set_result(("ok", {"doc_id": doc.doc_id}, (0, None)))
        return future

    def kill(self) -> None:
        raise AssertionError("no document overran")

    def restart(self) -> None:
        self.broken = False
        self.restarts += 1


class _HangingPool:
    """One worker, run inline. Document "hang" never finishes, and holds up all work submitted
    after it, until the pool's processes are killed."""

    def __init__(self) -> None:
        self.stuck: list[Future[Any]] = []
        self.kills = 0
        self.restarts = 0

    def submit(self, fn: Callable[..., Any], doc: DocumentInput) -> Future[Any]:
        future: Future[Any] = Future()
        if self.stuck or doc.doc_id == "hang":
            self.stuck.append(future)
        else:
            future.set_result(("ok", {"doc_id": doc.doc_id}, (0, None)))
        return future

    def kill(self) -> None:
        self.kills += 1
        for future in self.stuck:  # as ProcessPoolExecutor fails the futures of killed workers
            future.set_exception(BrokenProcessPool("a worker was terminated"))
        self.stuck = []

    def restart(self) -> None:
        self.restarts += 1


def test_a_document_that_overruns_the_timeout_is_recorded_and_the_rest_are_retried() -> None:
    docs = [DocumentInput(doc_id=k, text=k) for k in ("a", "hang", "b", "c", "hang", "d")]
    pool = _HangingPool()
    outcomes = list(_pooled(pool, docs, in_flight=3, timeout=0.05))
    assert [o[0] for o in outcomes] == ["ok", "error", "ok", "ok", "error", "ok"]
    assert outcomes[1][1] == {"doc_key": "hang", "stage": "extract", "error_type": "Timeout"}
    assert [o[1]["doc_id"] for o in outcomes if o[0] == "ok"] == ["a", "b", "c", "d"]
    assert pool.kills == pool.restarts == 2


def test_the_timeout_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timeout"):
        run_batch([], tmp_path, ExtractionConfig(), timeout=0)


def test_a_dead_worker_is_blamed_only_on_the_document_that_kills_it() -> None:
    docs = [DocumentInput(doc_id=k, text=k) for k in ("a", "b", "kill", "c", "d")]
    pool = _BreakingPool()
    outcomes = list(_pooled(pool, docs, in_flight=3))
    assert [o[0] for o in outcomes] == ["ok", "ok", "error", "ok", "ok"]
    assert outcomes[2][1] == {"doc_key": "kill", "stage": "extract", "error_type": "WorkerDied"}
    assert [o[1]["doc_id"] for i, o in enumerate(outcomes) if i != 2] == ["a", "b", "c", "d"]
    assert pool.restarts == 2  # once for the batch, once for the document that died alone
