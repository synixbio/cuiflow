"""The scripts in examples/, run against the fake engine and synthetic stores: no UMLS data."""

from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from cuiflow.core.config import ExtractionConfig
from cuiflow.core.models import (
    AssertionStatus,
    DocumentResult,
    Mention,
    TerminologyCode,
    make_mention_id,
)
from cuiflow.io.writers.jsonl import JsonlWriter
from tests.conftest import FakeEngine

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
# The sdist ships examples/ too; the skip covers copies of tests/ made without it.
pytestmark = pytest.mark.skipif(not EXAMPLES.is_dir(), reason="examples/ not present")
DIABETES = "C0011849"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def configs() -> list[ExtractionConfig]:
    """Every config an Extractor was built with, in order."""
    return []


@pytest.fixture
def noi(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configs: list[ExtractionConfig]
) -> ModuleType:
    module = _load("notes_of_interest")
    real = module.Extractor

    def factory(config: ExtractionConfig) -> Any:
        configs.append(config)
        return real(config, engine=FakeEngine(fail_on="EXPLODE"))

    monkeypatch.setattr(module, "Extractor", factory)
    for var in ("CUIFLOW_TERMINOLOGY_DB", "CUIFLOW_MODE", "CUIFLOW_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)  # no stray ./cuiflow.toml
    return module


@pytest.fixture
def notes(tmp_path: Path) -> Path:
    d = tmp_path / "notes"
    d.mkdir()
    (d / "a.txt").write_text("Chest pain since this morning.", encoding="utf-8")
    (d / "b.txt").write_text("Patient reports no chest pain.", encoding="utf-8")
    (d / "c.txt").write_text("Diabetes, on metformin.", encoding="utf-8")
    return d


def _mention(doc_id: str, cui: str = DIABETES, **assertions: Any) -> Mention:
    return Mention(
        mention_id=make_mention_id(doc_id, 0, 5, cui),
        start=0,
        end=5,
        text="x",
        cui=cui,
        preferred_name="x",
        semantic_group="DISORDER",
        assertions=AssertionStatus(**assertions),
    )


def _run(path: Path, results: list[DocumentResult], manifest: dict[str, Any] | None) -> Path:
    path.mkdir()
    writer = JsonlWriter(path / "results.jsonl")
    for r in results:
        writer.write(r)
    writer.close()
    if manifest is not None:
        (path / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_term_splits_affirmed_negated_and_absent(
    noi: ModuleType, notes: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    save = tmp_path / "cohort.sqlite"
    argv = [str(notes), "--term", "chest pain", "--save", str(save)]
    assert noi.main(argv) == 0
    out = capsys.readouterr().out
    assert "resolves to 1 concept(s)" in out and "C0008031" in out
    assert "narrower matches not searched: C0030193 Pain" in out
    assert "1 affirm the concept" in out
    assert "1 mention it but never affirm it" in out
    assert "1 do not mention it" in out
    # The fake engine, like mmlite with NegEx, assesses negation only.
    assert "not assessed for hedging, subject" in out

    with sqlite3.connect(save) as conn:
        statuses = dict(conn.execute("SELECT doc_id, status FROM documents").fetchall())
        assert {Path(k).name: v for k, v in statuses.items()} == {
            "a.txt": "affirmed",
            "b.txt": "excluded",
            "c.txt": "absent",  # the denominator keeps notes with no mention
        }
        row = conn.execute("SELECT query, mode, n_documents, n_errors FROM search").fetchone()
        assert row == ("term:chest pain", "single:umlsmatch", 3, 0)
    assert noi.main(argv) == 0  # appends a second search rather than overwriting
    with sqlite3.connect(save) as conn:
        assert conn.execute("SELECT COUNT(*) FROM search").fetchone() == (2,)


def test_one_engine_load_for_term_and_notes(
    noi: ModuleType, notes: Path, configs: list[ExtractionConfig]
) -> None:
    assert noi.main([str(notes), "--term", "chest pain"]) == 0
    assert len(configs) == 1
    assert configs[0].code_systems == ()  # matching is by CUI: no code lookups per note


def test_a_failing_note_is_an_error_row_not_a_crash(
    noi: ModuleType, notes: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (notes / "d.txt").write_text("EXPLODE with secret chest pain", encoding="utf-8")
    save = tmp_path / "s.sqlite"
    assert noi.main([str(notes), "--cui", "C0008031", "--save", str(save)]) == 1
    captured = capsys.readouterr()
    assert "1 could not be processed" in captured.out and "RuntimeError" in captured.out
    assert "secret" not in captured.out + captured.err  # the message quotes the note
    with sqlite3.connect(save) as conn:
        assert conn.execute(
            "SELECT status, error_type FROM documents WHERE doc_id LIKE '%d.txt'"
        ).fetchone() == ("error", "RuntimeError")
        assert conn.execute("SELECT n_documents, n_errors FROM search").fetchone() == (4, 1)


def test_classify_buckets_in_order(noi: ModuleType) -> None:
    doc = DocumentResult(
        "d",
        (
            _mention("d", negated=True, subject="family_member"),  # negation first
            _mention("d", negated=False, subject="family_member"),
            _mention("d", negated=False, subject="patient", uncertain=True),
            _mention("d", negated=False, temporality="hypothetical"),
            _mention("d", negated=False, subject="patient", generic=True),
            _mention("d", negated=False, subject="patient", uncertain=False, history_of=True),
            _mention("d", "C0000000", negated=False),  # not a target
        ),
    )
    s = noi.classify(doc, {DIABETES})
    assert (s.n_negated, s.n_other_subject, s.n_hedged, s.n_affirmed) == (1, 1, 3, 1)
    assert s.status == "affirmed" and s.unassessed == set()  # history is still affirmed

    hedged_only = DocumentResult("h", (_mention("h", negated=False, conditional=True),))
    assert noi.classify(hedged_only, {DIABETES}).status == "excluded"


def test_run_directory_uses_its_errors_and_its_config(
    noi: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    configs: list[ExtractionConfig],
) -> None:
    patient = {"negated": False, "subject": "patient", "uncertain": False}
    manifest = {
        "config": {"mode": "single:mmlite", "profile": "strict"},
        "errors": [
            {"doc_key": "broken", "stage": "extract", "error_type": "ValueError"},
            {"doc_key": "p", "stage": "extract", "error_type": "OSError"},  # fixed on resume
        ],
    }
    run = _run(
        tmp_path / "run",
        [
            DocumentResult("p", (_mention("p", **patient),)),
            DocumentResult("f", (_mention("f", negated=False, subject="family_member"),)),
            DocumentResult("n", ()),
        ],
        manifest,
    )
    assert noi.main([str(run), "--cui", DIABETES.lower(), "--show-excluded"]) == 1
    out = capsys.readouterr().out
    assert "Scanned 4 documents" in out
    assert "1 affirm the concept" in out
    assert "negated 0, someone else's 1, hedged 0" in out
    assert "broken" in out and "ValueError" in out and "OSError" not in out
    assert "not assessed" not in out
    assert configs == []  # --cui over a run loads no engine

    assert noi.main([str(run), "--term", "diabetes"]) == 1
    assert configs[-1].mode.value == "single:mmlite"  # the run's mode, not the default


def test_results_file_is_read_as_results_not_notes(
    noi: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _run(tmp_path / "run", [DocumentResult("p", (_mention("p", negated=False),))], None)
    assert noi.main([str(run / "results.jsonl"), "--cui", DIABETES]) == 0
    assert "1 affirm the concept" in capsys.readouterr().out


def test_code_resolves_through_the_store(
    noi: ModuleType, notes: Path, terminology_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [str(notes), "--code", "icd10cm:r07.9", "--terminology-db", str(terminology_db)]
    assert noi.main(argv) == 0
    captured = capsys.readouterr()
    assert "cover 1 concept(s)" in captured.out and "C0008031" in captured.out
    assert "1 affirm the concept" in captured.out
    assert "no ICD10CM hierarchy" in captured.err  # the fixture store was built without MRREL


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--code", "R07.9"], "SYSTEM:CODE"),
        (["--code", "MSH:D003920"], "has no MSH codes"),
    ],
)
def test_bad_code_is_a_usage_error(
    noi: ModuleType,
    notes: Path,
    terminology_db: Path,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    message: str,
) -> None:
    assert noi.main([str(notes), *args, "--terminology-db", str(terminology_db)]) == 2
    assert message in capsys.readouterr().err


def test_bad_input_fails_before_loading_an_engine(
    noi: ModuleType, tmp_path: Path, configs: list[ExtractionConfig]
) -> None:
    assert noi.main([str(tmp_path / "missing"), "--term", "chest pain"]) == 2
    assert configs == []
    with pytest.raises(SystemExit):
        noi.main([str(tmp_path), "--cui", "C12345"])  # not a CUI


# ---- note_summary, concept_matrix, highlight_note ---------------------------------------------


def _patched(monkeypatch: pytest.MonkeyPatch, name: str, configs: list[ExtractionConfig]) -> Any:
    module = _load(name)
    real = module.Extractor

    def factory(config: ExtractionConfig) -> Any:
        configs.append(config)
        return real(config, engine=FakeEngine(fail_on="EXPLODE"))

    monkeypatch.setattr(module, "Extractor", factory)
    for var in ("CUIFLOW_TERMINOLOGY_DB", "CUIFLOW_MODE", "CUIFLOW_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    return module


@pytest.fixture
def summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configs: list[ExtractionConfig]
) -> ModuleType:
    monkeypatch.chdir(tmp_path)  # no stray ./cuiflow.toml
    return _patched(monkeypatch, "note_summary", configs)


@pytest.fixture
def matrix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configs: list[ExtractionConfig]
) -> ModuleType:
    monkeypatch.chdir(tmp_path)
    return _patched(monkeypatch, "concept_matrix", configs)


@pytest.fixture
def highlight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configs: list[ExtractionConfig]
) -> ModuleType:
    monkeypatch.chdir(tmp_path)
    return _patched(monkeypatch, "highlight_note", configs)


def _coded(doc_id: str, cui: str, *codes: TerminologyCode, **assertions: Any) -> Mention:
    return replace(_mention(doc_id, cui, **assertions), codes=codes)


def test_summary_groups_concepts_by_status(
    summary: ModuleType, notes: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert summary.main([str(notes), "--no-codes"]) == 0
    out = capsys.readouterr().out
    a, b, c = out.split("== ")[1:]
    assert "a.txt" in a and "affirmed:" in a and "Chest Pain" in a
    assert "b.txt" in b and "negated:" in b and "Chest Pain" in b and "affirmed:" not in b
    assert "Problems (DISORDER)" in c and "Medications (DRUG)" in c
    assert c.index("Problems") < c.index("Medications")
    assert "hedging, subject not assessed" in a  # the fake engine assesses negation only


def test_summary_reads_a_run_without_an_engine(
    summary: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    configs: list[ExtractionConfig],
) -> None:
    patient = {"negated": False, "subject": "patient", "uncertain": False}
    snomed = TerminologyCode("SNOMEDCT_US", "73211009", "PT", "Diabetes mellitus", True)
    product = TerminologyCode(
        "RXNORM", "861007", "SCD", "metformin 500 MG", True, ingredients=("6809",)
    )
    run = _run(
        tmp_path / "run",
        [
            DocumentResult(
                "p",
                (
                    _coded("p", DIABETES, snomed, **patient),
                    _coded("p", DIABETES, snomed, negated=True, subject="patient"),
                    _mention("p", "C0000001", negated=False, subject="family_member"),
                    replace(_coded("p", "C0025598", product, **patient), semantic_group="DRUG"),
                ),
            )
        ],
        {"errors": [{"doc_key": "broken", "stage": "extract", "error_type": "ValueError"}]},
    )
    assert summary.main([str(run)]) == 1
    out = capsys.readouterr().out
    # Named by the SNOMED display, not the engine's "x"; the negated mention is noted, not lost.
    assert "Diabetes mellitus x2  (also negated 1)  [SNOMEDCT_US 73211009]" in out
    assert "someone else's:" in out
    assert "[RXNORM 861007; ingredient RXNORM 6809]" in out
    assert "could not be processed: ValueError" in out
    assert configs == []

    assert summary.main([str(run), "--json", "--groups", "disorder"]) == 1
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[1] == {"doc_id": "broken", "error_type": "ValueError"}
    diabetes = records[0]["concepts"][0]
    assert diabetes["status"] == "affirmed" and diabetes["engine_name"] == "x"
    assert diabetes["counts"] == {"affirmed": 1, "negated": 1, "other_subject": 0, "hedged": 0}
    assert {c["cui"] for c in records[0]["concepts"]} == {DIABETES, "C0000001"}  # no DRUG


def test_summary_codes_from_the_store_and_errors_by_type(
    summary: ModuleType,
    notes: Path,
    terminology_db: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (notes / "d.txt").write_text("EXPLODE with secret chest pain", encoding="utf-8")
    argv = [str(notes), "--codes", "SNOMEDCT_US,ICD10CM", "--terminology-db", str(terminology_db)]
    assert summary.main(argv) == 1
    captured = capsys.readouterr()
    assert "Chest pain  [ICD10CM R07.9, SNOMEDCT_US 29857009]" in captured.out
    assert "could not be processed: RuntimeError" in captured.out
    assert "secret" not in captured.out + captured.err
    assert "summarized 3 of 4 documents" in captured.err


def test_matrix_cells_and_denominator(
    matrix: ModuleType, notes: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (notes / "d.txt").write_text("Unremarkable.", encoding="utf-8")
    out = tmp_path / "m" / "features.csv"
    assert matrix.main([str(notes), "-o", str(out)]) == 0
    with out.open(encoding="utf-8", newline="") as fh:
        rows = {Path(r["doc_id"]).name: r for r in csv.DictReader(fh)}
    assert set(rows) == {"a.txt", "b.txt", "c.txt", "d.txt"}  # the empty note keeps its row
    assert (rows["a.txt"]["C0008031"], rows["b.txt"]["C0008031"]) == ("1", "-1")
    assert rows["c.txt"]["C0008031"] == "0" and rows["c.txt"][DIABETES] == "1"
    assert set(rows["d.txt"].values()) - {rows["d.txt"]["doc_id"]} == {"0"}
    with (out.parent / "features.concepts.csv").open(encoding="utf-8", newline="") as fh:
        concepts = {r["cui"]: r for r in csv.DictReader(fh)}
    assert concepts["C0008031"]["n_affirmed"] == "1"
    assert concepts["C0008031"]["n_not_affirmed"] == "1"
    report = capsys.readouterr().out
    assert "4 documents x" in report and "1 documents have no concept" in report
    assert "Most often mentioned without being affirmed" in report

    assert matrix.main([str(notes), "-o", str(out), "--min-docs", "2"]) == 0
    assert out.read_text(encoding="utf-8").splitlines()[0] == "doc_id"  # nothing in 2 notes


def test_matrix_leaves_failed_notes_out_and_says_so(
    matrix: ModuleType, notes: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (notes / "d.txt").write_text("EXPLODE with secret chest pain", encoding="utf-8")
    out = tmp_path / "features.csv"
    assert matrix.main([str(notes), "-o", str(out)]) == 1
    captured = capsys.readouterr()
    assert "d.txt" not in out.read_text(encoding="utf-8")  # not an all-zero row
    assert "1 document(s) could not be processed" in captured.err
    assert "RuntimeError" in captured.err and "secret" not in captured.out + captured.err


def test_matrix_from_a_run_keeps_duplicate_keys_apart(
    matrix: ModuleType, tmp_path: Path, configs: list[ExtractionConfig]
) -> None:
    run = _run(
        tmp_path / "run",
        [
            DocumentResult("same", (_mention("same", negated=False),)),
            DocumentResult("same", (_mention("same", negated=False, subject="other"),)),
        ],
        None,
    )
    out = tmp_path / "features.csv"
    assert matrix.main([str(run), "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8").splitlines()[1:] == ["same,1", "same#1,-1"]
    assert configs == []


def test_highlight_segments_split_overlaps(highlight: ModuleType) -> None:
    outer = replace(_mention("d", "C0008031"), start=0, end=10)
    inner = replace(_mention("d", "C0030193"), start=6, end=10)
    segs = list(highlight.segments("chest pain now", [inner, outer]))
    assert [(s.start, s.end, [m.cui for m in s.mentions]) for s in segs] == [
        (0, 6, ["C0008031"]),
        (6, 10, ["C0008031", "C0030193"]),  # longest first
        (10, 14, []),
    ]


def test_highlight_page(
    highlight: ModuleType, notes: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (notes / "b.txt").write_text("<b>No chest pain</b> today.", encoding="utf-8")
    (notes / "d.txt").write_text("EXPLODE with secret chest pain", encoding="utf-8")
    out = tmp_path / "review.html"
    assert highlight.main([str(notes), "-o", str(out)]) == 1
    page = out.read_text(encoding="utf-8")
    assert '<mark class="st-affirmed" title="C0008031 Chest Pain (FINDING): affirmed' in page
    assert '<mark class="st-negated nested"' in page  # "pain" inside "chest pain"
    assert "&lt;b&gt;" in page and "<b>No" not in page  # note text is escaped
    assert "Could not be processed: RuntimeError" in page and "secret" not in page
    assert "Contains note text: PHI" in page
    assert "4 notes, 1 failed" in capsys.readouterr().err

    assert highlight.main([str(notes), "-o", str(out), "--limit", "1"]) == 0
    assert out.read_text(encoding="utf-8").count("<section>") == 1
