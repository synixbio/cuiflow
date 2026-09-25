from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from cuiflow import __version__
from cuiflow.interfaces.cli import main
from tests.conftest import FakeEngine


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_extract_to_stdout(
    tmp_path: Path, patch_extractor: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    patch_extractor()
    note = tmp_path / "n.txt"
    note.write_text("No chest pain.", encoding="utf-8")
    assert main(["extract", str(note), "--groups", "FINDING"]) == 0
    (line,) = capsys.readouterr().out.splitlines()
    result = json.loads(line)
    assert {m["cui"] for m in result["mentions"]} == {"C0008031", "C0030193"}


def test_extract_run_dir_then_inspect(
    tmp_path: Path, patch_extractor: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    patch_extractor()
    note = tmp_path / "n.txt"
    note.write_text("metformin", encoding="utf-8")
    runs = tmp_path / "runs"
    assert main(["extract", str(note), "--out-dir", str(runs), "--run-id", "r"]) == 0
    capsys.readouterr()
    assert main(["inspect-manifest", str(runs / "r")]) == 0
    assert json.loads(capsys.readouterr().out)["documents"] == 1


def test_build_terminology(rrf_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "t.sqlite"
    args = [
        "build-terminology",
        "--mrconso",
        str(rrf_dir / "MRCONSO.RRF"),
        "--mrrank",
        str(rrf_dir / "MRRANK.RRF"),
        "--out",
        str(out),
    ]
    assert main(args) == 0 and out.is_file()
    assert main(args) == 2  # refuses to overwrite, with a message rather than a traceback


def test_bad_config_is_a_clean_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["info", "--codes", "SNOMED"]) == 2
    assert "unknown code system" in capsys.readouterr().err


def test_missing_extra_is_a_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import cuiflow.api

    def no_engine(*args: object, **kwargs: object) -> None:
        raise ImportError("the umlsmatch engine needs the 'umlsmatch' extra")

    monkeypatch.setattr(cuiflow.api, "Extractor", no_engine)
    monkeypatch.setattr(sys, "stdin", io.StringIO("Denies chest pain."))
    assert main(["extract", "-"]) == 2
    assert "needs the 'umlsmatch' extra" in capsys.readouterr().err


@pytest.mark.parametrize("to_file", [False, True])
def test_one_shot_extract_isolates_a_failing_document(
    tmp_path: Path,
    patch_extractor: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
    to_file: bool,
) -> None:
    patch_extractor(fail_on="SECRET")
    (tmp_path / "a.txt").write_text("SECRET-NOTE chest pain", encoding="utf-8")
    (tmp_path / "b.txt").write_text("No chest pain.", encoding="utf-8")
    out = tmp_path / "out.jsonl"
    assert main(["extract", str(tmp_path), *(["-o", str(out)] if to_file else [])]) == 1
    captured = capsys.readouterr()
    lines = out.read_text(encoding="utf-8") if to_file else captured.out
    assert [json.loads(line)["doc_id"] for line in lines.splitlines()] == [
        str(tmp_path / "b.txt")
    ]  # the document after the failing one is still done
    assert "RuntimeError" in captured.err and "1 documents failed" in captured.err
    assert "SECRET" not in captured.err  # the exception type only, never its message


def test_one_shot_output_appears_only_when_complete(
    tmp_path: Path,
    patch_extractor: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_extractor()
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.txt").write_text("chest pain", encoding="utf-8")
    out = tmp_path / "out.jsonl"

    def interrupted(*_: object) -> None:
        raise KeyboardInterrupt

    with monkeypatch.context() as m:
        m.setattr(FakeEngine, "extract", interrupted)
        with pytest.raises(KeyboardInterrupt):
            main(["extract", str(tmp_path / "notes"), "-o", str(out)])
    assert list(tmp_path.glob("*.jsonl")) == []  # neither the output nor its partial file

    assert main(["extract", str(tmp_path / "notes"), "-o", str(out)]) == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1
    assert main(["extract", str(tmp_path / "notes"), "-o", str(out)]) == 2  # never overwritten
    assert "exists" in capsys.readouterr().err


def test_one_shot_extract_reports_unreadable_input_and_skipped_files(
    tmp_path: Path, patch_extractor: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    patch_extractor()
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a.txt").write_text("chest pain", encoding="utf-8")
    (tmp_path / "d" / "notes.jsonl").write_text("", encoding="utf-8")
    j = tmp_path / "in.jsonl"
    j.write_text('{"id": "x", "text": "SECRET\n{"id": "y", "text": "ok"}\n', encoding="utf-8")
    assert main(["extract", str(tmp_path / "d"), str(j)]) == 1
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 2
    assert "notes.jsonl" in captured.err and "not read" in captured.err
    assert '"stage": "read"' in captured.err and "SECRET" not in captured.err


def test_workers_without_a_run_directory_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extract", "-", "--workers", "2"]) == 2
    assert "--out-dir" in capsys.readouterr().err


def test_timeout_without_a_run_directory_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extract", "-", "--timeout", "60"]) == 2
    assert "--out-dir" in capsys.readouterr().err
