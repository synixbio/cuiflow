"""The Phase 4 resume gate: a killed run resumes without duplicating documents (DESIGN_PLAN §11).

One test kills a real batch process mid-run (TerminateProcess on Windows, SIGKILL elsewhere:
no cleanup runs), then resumes it in-process. The notes are invented.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from cuiflow.core.config import ExtractionConfig
from cuiflow.core.enums import OverlapPolicy
from cuiflow.interfaces.batch import RESULTS_NAME, _truncate_torn_tail, run_batch
from cuiflow.interfaces.export import export_results
from cuiflow.io.writers.jsonl import read_jsonl

ROOT = Path(__file__).resolve().parents[1]

# Runs a batch with a FakeEngine that sleeps per document, so the parent can kill it mid-run.
CHILD = """
import sys, time
from pathlib import Path
import cuiflow.interfaces.batch as batch
from cuiflow.api import Extractor
from cuiflow.core.config import ExtractionConfig
from tests.conftest import FakeEngine

class Slow(FakeEngine):
    def extract(self, text, doc_id):
        time.sleep(0.05)
        return super().extract(text, doc_id)

batch.Extractor = lambda config: Extractor(config, engine=Slow())
batch.CHECKPOINT_EVERY = 5
batch.run_batch([sys.argv[1]], Path(sys.argv[2]), ExtractionConfig(), run_id="r")
"""


def _notes(root: Path, n: int) -> Path:
    root.mkdir()
    for i in range(n):
        (root / f"n{i:03d}.txt").write_text(f"Note {i}. No chest pain. Diabetes.", encoding="utf-8")
    return root


def _lines(path: Path) -> int:
    return path.read_bytes().count(b"\n") if path.is_file() else 0


def test_killed_run_resumes_without_duplicates(
    tmp_path: Path, patch_extractor: Callable[..., None]
) -> None:
    notes = _notes(tmp_path / "notes", 60)
    runs = tmp_path / "runs"
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(notes), str(runs)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    results = runs / "r" / RESULTS_NAME
    deadline = time.monotonic() + 60
    while _lines(results) < 10:
        assert child.poll() is None, child.stderr.read().decode() if child.stderr else ""
        assert time.monotonic() < deadline, "the batch never got going"
        time.sleep(0.02)
    child.kill()  # hard kill: no finally blocks, no manifest update
    child.wait()
    done_before = _lines(results)
    assert 10 <= done_before < 60
    killed = json.loads((runs / "r" / "run_manifest.json").read_text(encoding="utf-8"))
    assert killed["finished_at"] is None  # the manifest shows the run did not finish
    assert killed["documents"] >= 5  # checkpointed counts survive the kill

    # Simulate the worst case too: the kill landed mid-line.
    with results.open("ab") as fh:
        fh.write(b'{"doc_id": "half-writ')

    patch_extractor()
    run_batch([notes], runs, ExtractionConfig(), resume=runs / "r")

    docs = [r.doc_id for r in read_jsonl(results)]
    assert len(docs) == 60 and Counter(docs).most_common(1)[0][1] == 1  # all, once each
    manifest = json.loads((runs / "r" / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["skipped_resumed"] == done_before
    assert manifest["documents"] == 60 - done_before
    assert manifest["documents_in_results"] == 60
    (killed_session,) = manifest["previous_sessions"]
    assert killed_session["finished_at"] is None
    assert killed_session["started_at"] == killed["started_at"]
    assert killed_session["documents"] == killed["documents"]


def test_truncate_torn_tail(tmp_path: Path) -> None:
    p = tmp_path / "r.jsonl"
    p.write_bytes(b'{"a": 1}\n{"b": 2}\n{"c": ')
    assert _truncate_torn_tail(p) == 6
    assert p.read_bytes() == b'{"a": 1}\n{"b": 2}\n'
    assert _truncate_torn_tail(p) == 0
    p.write_bytes(b'{"only": "torn"')
    _truncate_torn_tail(p)
    assert p.read_bytes() == b""


def test_resume_refuses_different_settings(
    tmp_path: Path, patch_extractor: Callable[..., None]
) -> None:
    patch_extractor()
    notes = _notes(tmp_path / "notes", 2)
    run_dir = run_batch([notes], tmp_path / "runs", ExtractionConfig())
    with pytest.raises(ValueError, match="configuration differs"):
        run_batch(
            [notes],
            tmp_path / "runs",
            ExtractionConfig(overlaps=OverlapPolicy.LONGEST),
            resume=run_dir,
        )


def test_resume_keeps_history_and_exports(
    tmp_path: Path, patch_extractor: Callable[..., None]
) -> None:
    patch_extractor()
    notes = _notes(tmp_path / "notes", 2)
    run_dir = run_batch([notes], tmp_path / "runs", ExtractionConfig())
    export_results(run_dir, tmp_path / "m.sqlite")
    (notes / "extra.txt").write_text("Metformin.", encoding="utf-8")
    run_batch([notes], tmp_path / "runs", ExtractionConfig(), resume=run_dir)
    run_batch([notes], tmp_path / "runs", ExtractionConfig(), resume=run_dir)

    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert [s["documents"] for s in manifest["previous_sessions"]] == [2, 1]
    assert manifest["documents"] == 0 and manifest["skipped_resumed"] == 3
    assert manifest["documents_in_results"] == 3
    assert [e["format"] for e in manifest["exports"]] == ["sqlite"]


def test_done_keys_index_is_on_disk_and_removed(tmp_path: Path) -> None:
    from cuiflow.interfaces.batch import DONE_INDEX_NAME, DoneKeys

    results = tmp_path / RESULTS_NAME
    rows = [
        {"doc_id": "a", "metadata": {"content_sha256": "h1"}},
        {"doc_id": "a", "metadata": {"content_sha256": "h1"}},  # a duplicate line counts once
        {"doc_id": "b", "metadata": {"content_sha256": "h2"}},
    ]
    results.write_text("".join(json.dumps(r) + "\n" for r in rows) + "\n", encoding="utf-8")
    (tmp_path / DONE_INDEX_NAME).write_bytes(b"stale junk from a killed session")

    done = DoneKeys(results)
    assert (tmp_path / DONE_INDEX_NAME).is_file()
    assert len(done) == 2
    assert ("a", "h1") in done and ("b", "h2") in done
    assert ("a", "h2") not in done  # same key, changed text: redone
    done.close()
    assert not (tmp_path / DONE_INDEX_NAME).exists()

    empty = DoneKeys(tmp_path / "missing.jsonl")
    assert len(empty) == 0 and ("a", "h1") not in empty
    empty.close()


def test_worker_reports_its_pid_and_peak_rss(patch_extractor: Callable[..., None]) -> None:
    import os

    import cuiflow.interfaces.batch as batch
    from cuiflow.api import DocumentInput

    patch_extractor()
    batch._init_worker(ExtractionConfig())
    try:
        status, payload, (pid, rss) = batch._process_one(DocumentInput("d", "No chest pain."))
    finally:
        assert batch._worker_extractor is not None
        batch._worker_extractor.close()
        batch._worker_extractor = None
    assert status == "ok" and payload["doc_id"] == "d"
    assert pid == os.getpid() and (rss is None or rss > 0)


def test_resume_refuses_inputs_named_differently(
    tmp_path: Path, patch_extractor: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_extractor()
    notes = _notes(tmp_path / "notes", 3)
    run_dir = run_batch([notes], tmp_path / "runs", ExtractionConfig())
    before = (run_dir / RESULTS_NAME).read_bytes()
    monkeypatch.chdir(tmp_path)
    # The same files, but keyed "notes/n000.txt" rather than the absolute path: all would be
    # done again and appended as duplicates.
    with pytest.raises(ValueError, match="as named before"):
        run_batch([Path("notes")], tmp_path / "runs", ExtractionConfig(), resume=run_dir)
    assert (run_dir / RESULTS_NAME).read_bytes() == before


def test_resume_of_jsonl_input_does_not_depend_on_its_path(
    tmp_path: Path, patch_extractor: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_extractor()
    src = tmp_path / "notes.jsonl"
    src.write_text(
        "".join(json.dumps({"id": f"d{i}", "text": "No chest pain."}) + "\n" for i in range(3)),
        encoding="utf-8",
    )
    run_dir = run_batch([src], tmp_path / "runs", ExtractionConfig())
    monkeypatch.chdir(tmp_path)
    run_batch([Path("notes.jsonl")], tmp_path / "runs", ExtractionConfig(), resume=run_dir)
    assert len(list(read_jsonl(run_dir / RESULTS_NAME))) == 3  # keys are the records' ids


def test_resume_refuses_a_changed_engine_and_keeps_the_manifest(
    tmp_path: Path, patch_extractor: Callable[..., None]
) -> None:
    patch_extractor()
    notes = _notes(tmp_path / "notes", 2)
    run_dir = run_batch([notes], tmp_path / "runs", ExtractionConfig())
    path = run_dir / "run_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["engines"]["engine"]["umls_release"] = "2025AB"  # as if rebuilt in place
    path.write_text(json.dumps(manifest), encoding="utf-8")
    edited = path.read_bytes()
    with pytest.raises(ValueError, match="engine differ"):
        run_batch([notes], tmp_path / "runs", ExtractionConfig(), resume=run_dir)
    assert path.read_bytes() == edited  # the refused session left no trace


def test_one_session_at_a_time_holds_a_run(
    tmp_path: Path, patch_extractor: Callable[..., None]
) -> None:
    from cuiflow.interfaces.batch import RunInUse, RunLock

    patch_extractor()
    notes = _notes(tmp_path / "notes", 2)
    run_dir = run_batch([notes], tmp_path / "runs", ExtractionConfig())
    held = RunLock(run_dir)  # another session, still running
    try:
        with pytest.raises(RunInUse):
            run_batch([notes], tmp_path / "runs", ExtractionConfig(), resume=run_dir)
    finally:
        held.release()
    run_batch([notes], tmp_path / "runs", ExtractionConfig(), resume=run_dir)
    RunLock(run_dir).release()  # the session released it


def test_done_key_reads_written_lines_without_a_full_parse() -> None:
    from cuiflow.core.models import AssertionStatus, DocumentResult, Mention, make_mention_id
    from cuiflow.interfaces.batch import _done_key

    trap = 'a "content_sha256": "' + "0" * 64 + '" trap é'  # escaped in JSON: never a key
    mention = Mention(make_mention_id("d", 0, 3, "C1"), 0, 3, trap, "C1", trap, "FINDING",
                      AssertionStatus())  # fmt: skip
    for doc_id in ["notes/a.txt", 'odd "id" \\ é', trap]:
        result = DocumentResult(
            doc_id, (mention,), text=trap, metadata={"content_sha256": "f" * 64, "note": trap}
        )
        line = json.dumps(result.to_dict(), ensure_ascii=False)
        assert _done_key(line) == (doc_id, "f" * 64)
    assert _done_key(json.dumps({"doc_id": "x"})) == ("x", "")  # falls back to parsing
