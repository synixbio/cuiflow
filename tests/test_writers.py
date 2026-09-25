"""Parquet, SQLite and OMOP NOTE_NLP writers, and ``cuiflow export`` (DESIGN_PLAN §8)."""

from __future__ import annotations

import csv
import dataclasses
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cuiflow import __version__
from cuiflow.core.models import (
    AssertionStatus,
    DocumentResult,
    Mention,
    ProvenanceInfo,
    TerminologyCode,
)
from cuiflow.interfaces.cli import main
from cuiflow.interfaces.export import export_results, note_id_from_doc_id
from cuiflow.io.writers import open_writer
from cuiflow.io.writers.jsonl import iter_jsonl
from cuiflow.io.writers.omop import COLUMNS, choose_code, snippet
from cuiflow.terminology.omop import OmopVocabulary, build_omop_vocabulary

TEXT = "Denies chest pain. Takes metformin."


def _code(system: str, code: str, preferred: bool = True) -> TerminologyCode:
    return TerminologyCode(system, code, "PT", f"{system} {code}", preferred, system == "ICD10CM")


def _result(doc_id: str = "notes/a.txt", *, text: str | None = TEXT) -> DocumentResult:
    chest = Mention(
        "m1",
        7,
        17,
        "chest pain",
        "C0008031",
        "Chest Pain",
        "FINDING",
        AssertionStatus(negated=True, subject="patient"),  # history_of etc. not assessed
        codes=(_code("ICD10CM", "R07.9"), _code("SNOMEDCT_US", "29857009")),
        provenance=ProvenanceInfo(
            found_by=("mmlite", "umlsmatch"),
            engine_assertions={"mmlite": {"negated": True}},
        ),
    )
    metformin = Mention(
        "m2",
        25,
        34,
        "metformin",
        "C0025598",
        "metformin",
        "DRUG",
        AssertionStatus(negated=False),
        codes=(_code("RXNORM", "6809"),),
        provenance=ProvenanceInfo(found_by=("umlsmatch",)),
    )
    return DocumentResult(
        doc_id=doc_id,
        mentions=(chest, metformin),
        text=text,
        metadata={"mode": "ensemble:union", "profile": "strict", "content_sha256": "ab"},
    )


def test_parquet_one_row_per_mention_keeps_nulls(tmp_path: Path) -> None:
    pq = pytest.importorskip("pyarrow.parquet")
    path = tmp_path / "m.parquet"
    w = open_writer(path)
    w.write(_result())
    w.write(DocumentResult(doc_id="empty", mentions=()))
    w.close()
    rows = pq.read_table(path).to_pylist()
    assert [r["cui"] for r in rows] == ["C0008031", "C0025598"]
    chest = rows[0]
    assert chest["assertions"]["negated"] is True
    assert chest["assertions"]["history_of"] is None  # not assessed stays null, never False
    assert [c["code"] for c in chest["codes"]] == ["R07.9", "29857009"]
    assert chest["provenance"]["found_by"] == ["mmlite", "umlsmatch"]
    assert json.loads(chest["provenance"]["engine_assertions"]) == {"mmlite": {"negated": True}}
    assert chest["mode"] == "ensemble:union"
    with pytest.raises(FileExistsError):
        open_writer(path)


def test_sqlite_layout_and_nullable_negation(tmp_path: Path) -> None:
    path = tmp_path / "a.sqlite"
    w = open_writer(path)
    w.write(_result())
    unassessed = Mention("m3", 0, 4, "pain", "C0030193", "Pain", "FINDING", AssertionStatus())
    w.write(DocumentResult(doc_id="b", mentions=(unassessed,)))
    w.close()
    con = sqlite3.connect(path)
    docs = con.execute("SELECT source, n_annotations FROM documents ORDER BY doc_id").fetchall()
    assert docs == [("notes/a.txt", 2), ("b", 1)]
    rows = con.execute(
        "SELECT cui, negated, subject, history_of, found_by FROM annotations ORDER BY id"
    ).fetchall()
    assert rows == [
        ("C0008031", 1, "patient", None, "mmlite,umlsmatch"),
        ("C0025598", 0, None, None, "umlsmatch"),
        ("C0030193", None, None, None, ""),  # negation not assessed: NULL, not 0
    ]
    assert con.execute("SELECT COUNT(*) FROM codes").fetchone() == (3,)


def test_snippet_and_code_choice() -> None:
    text = "x" * 300 + "chest pain" + "y" * 300
    s = snippet(text, 300, 310)
    assert len(s) == 250 and "chest pain" in s
    assert snippet("short chest pain", 6, 16) == "short chest pain"
    # SNOMED CT before ICD-10-CM, whatever order the codes came in.
    assert choose_code(_result().mentions[0]) == [("SNOMEDCT_US", "29857009"), ("ICD10CM", "R07.9")]


@pytest.fixture
def athena(tmp_path: Path) -> Path:
    """A tiny, invented Athena-shaped folder (tab-separated, no quoting)."""
    d = tmp_path / "athena"
    d.mkdir()
    header = (
        "concept_id\tconcept_name\tdomain_id\tvocabulary_id\tconcept_class_id\t"
        "standard_concept\tconcept_code\tvalid_start_date\tvalid_end_date\tinvalid_reason\n"
    )
    rows = [
        ("77670", 'Chest "pain"', "Condition", "SNOMED", "Clinical Finding", "S", "29857009"),
        ("35211388", "Chest pain, unsp", "Condition", "ICD10CM", "5-char code", "", "R07.9"),
        ("1503297", "metformin", "Drug", "RxNorm", "Ingredient", "S", "6809"),
        ("999", "Other vocab", "Drug", "NDC", "11-digit NDC", "", "6809"),
    ]
    (d / "CONCEPT.csv").write_text(
        header + "".join("\t".join(r) + "\t19700101\t20991231\t\n" for r in rows), encoding="utf-8"
    )
    (d / "CONCEPT_RELATIONSHIP.csv").write_text(
        "concept_id_1\tconcept_id_2\trelationship_id\tvalid_start_date\tvalid_end_date\t"
        "invalid_reason\n"
        "35211388\t77670\tMaps to\t19700101\t20991231\t\n"
        "35211388\t12345\tMaps to\t19700101\t20991231\tD\n",  # deprecated: ignored
        encoding="utf-8",
    )
    (d / "VOCABULARY.csv").write_text(
        "vocabulary_id\tvocabulary_name\tvocabulary_reference\tvocabulary_version\t"
        "vocabulary_concept_id\nSNOMED\tSNOMED\tx\t2026-03-01 SNOMED CT\t44819097\n",
        encoding="utf-8",
    )
    return d


def test_omop_vocabulary_lookup(athena: Path, tmp_path: Path) -> None:
    db = build_omop_vocabulary(athena, tmp_path / "v.sqlite")
    with OmopVocabulary(db) as v:
        assert v.lookup("SNOMEDCT_US", "29857009") == (77670, 77670)  # standard: itself
        assert v.lookup("ICD10CM", "R07.9") == (35211388, 77670)  # source -> Maps to
        assert v.lookup("RXNORM", "nope") == (0, 0)
        assert v.lookup("MSH", "D1") == (0, 0)  # not a vocabulary cuiflow maps
        assert v.build_info()["vocabularies"] == {"SNOMED": "2026-03-01 SNOMED CT"}
    with pytest.raises(FileExistsError):
        build_omop_vocabulary(athena, db)


# The OMOP CDM v5.4 NOTE_NLP DDL (OHDSI CommonDataModel, inst/ddl/5.4), in SQLite types with
# what PostgreSQL enforces added as CHECKs: SQLite ignores varchar(n), and its INTEGER is 64-bit
# where v5.4's integer is 32-bit.
INT32 = "BETWEEN -2147483648 AND 2147483647"
NOTE_NLP_V54 = f"""
CREATE TABLE note_nlp (
    note_nlp_id INTEGER NOT NULL CHECK (note_nlp_id {INT32}),
    note_id INTEGER NOT NULL CHECK (note_id {INT32}),
    section_concept_id INTEGER NULL,
    snippet TEXT NULL CHECK (length(snippet) <= 250),
    "offset" TEXT NULL CHECK (length("offset") <= 50),
    lexical_variant TEXT NOT NULL CHECK (length(lexical_variant) <= 250),
    note_nlp_concept_id INTEGER NULL,
    note_nlp_source_concept_id INTEGER NULL,
    nlp_system TEXT NULL CHECK (length(nlp_system) <= 250),
    nlp_date TEXT NOT NULL CHECK (nlp_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    nlp_datetime TEXT NULL,
    term_exists TEXT NULL CHECK (length(term_exists) <= 1),
    term_temporal TEXT NULL CHECK (length(term_temporal) <= 50),
    term_modifiers TEXT NULL CHECK (length(term_modifiers) <= 2000)
);
"""


def _load_note_nlp(csv_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(NOTE_NLP_V54)
    with csv_path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        assert tuple(next(reader)) == COLUMNS
        con.executemany(
            f"INSERT INTO note_nlp VALUES ({','.join('?' * len(COLUMNS))})",
            ([v if v != "" else None for v in row] for row in reader),
        )
    return con


def _run_dir(tmp_path: Path, results: list[DocumentResult]) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    w = open_writer(run / "results.jsonl")
    for r in results:
        w.write(r)
    w.close()
    (run / "run_manifest.json").write_text(
        json.dumps({"started_at": "2026-09-23T10:00:00Z", "config": {}}), encoding="utf-8"
    )
    return run


def test_omop_export_loads_into_v54_schema(athena: Path, tmp_path: Path) -> None:
    run = _run_dir(tmp_path, [_result("notes/a.txt"), _result("notes/b.txt", text=None)])
    vocab = build_omop_vocabulary(athena, tmp_path / "v.sqlite")
    out = tmp_path / "note_nlp.csv"
    summary = export_results(
        run,
        out,
        note_ids={"notes/a.txt": 1001, "notes/b.txt": 1002}.get,
        omop_vocab=vocab,
        snippets=True,
    )
    assert summary["rows"] == 4 and summary["rows_with_concept_id"] == 4
    assert summary["snippets"] == "on; 2 rows had no text"  # b was stored without its text

    con = _load_note_nlp(out)
    rows = con.execute(
        "SELECT note_id, lexical_variant, note_nlp_concept_id, note_nlp_source_concept_id, "
        "term_exists, term_temporal, nlp_date, nlp_system, snippet FROM note_nlp "
        'ORDER BY note_id, "offset" + 0'
    ).fetchall()
    assert rows[0] == (
        1001,
        "chest pain",
        77670,
        77670,  # SNOMED CT chosen before the ICD-10-CM crosswalk
        "N",
        None,  # history_of not assessed: NULL
        "2026-09-23",  # the run's start, from its manifest
        f"cuiflow {__version__} ensemble:union strict",
        TEXT,
    )
    assert rows[1][2:5] == (1503297, 1503297, "Y")
    assert rows[2][-1] is None  # no text stored: no snippet
    ids = [r[0] for r in con.execute("SELECT note_nlp_id FROM note_nlp ORDER BY note_nlp_id")]
    assert ids == [1, 2, 3, 4]  # sequential: fits v5.4's 32-bit integer
    assert summary["note_nlp_id"] == "sequential 1..4"

    manifest = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))
    (export,) = manifest["exports"]
    assert export["format"] == "omop" and export["concept_ids"] == "from the OMOP vocabulary"
    assert json.loads(Path(f"{out}.export.json").read_text(encoding="utf-8"))["rows"] == 4


def test_omop_without_vocabulary_or_note_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _run_dir(tmp_path, [_result("1"), _result("notes/x.txt")])
    out = tmp_path / "n.csv"
    # Integer document keys become note_id; a path does not, and is refused, not guessed.
    assert main(["export", str(run), "-o", str(out), "--note-id-from-doc-id"]) == 1
    err = capsys.readouterr().err
    assert "1 document(s) had no note_id" in err and "notes/x.txt" not in err
    con = _load_note_nlp(out)
    assert con.execute(
        "SELECT DISTINCT note_id, note_nlp_concept_id, note_nlp_source_concept_id FROM note_nlp"
    ).fetchall() == [(1, 0, 0)]
    summary = json.loads(Path(f"{out}.export.json").read_text(encoding="utf-8"))
    assert summary["concept_ids"].startswith("0: no OMOP vocabulary")


def test_export_formats_and_refusals(tmp_path: Path) -> None:
    run = _run_dir(tmp_path, [_result()])
    assert main(["export", str(run), "-o", str(tmp_path / "m.sqlite")]) == 0
    with pytest.raises(ValueError, match="note_ids"):
        export_results(run, tmp_path / "x.csv")  # .csv means OMOP, which needs note IDs
    with pytest.raises(ValueError, match="omop only"):
        export_results(run, tmp_path / "y.sqlite", snippets=True)
    with pytest.raises(ValueError, match="note_id"):
        open_writer(tmp_path / "z.csv")
    assert main(["export", str(run), "-o", str(tmp_path / "m.sqlite")]) == 2  # never overwrite


def test_failed_export_leaves_no_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cuiflow.io.writers.jsonl import JsonlWriter

    run = _run_dir(tmp_path, [_result("a"), _result("b")])
    calls = 0
    real = JsonlWriter.write

    def fail_second(self: JsonlWriter, result: DocumentResult) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        real(self, result)

    monkeypatch.setattr(JsonlWriter, "write", fail_second)
    out = tmp_path / "copy.jsonl.gz"
    with pytest.raises(OSError, match="disk full"):
        export_results(run, out)
    # Neither the output, its sidecar nor the partial file: a rerun just works.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["run"]
    monkeypatch.undo()
    assert export_results(run, out)["documents"] == 2
    assert [r.doc_id for r in iter_jsonl(out)] == ["a", "b"]  # still gzip: suffix kept


def test_export_refuses_a_run_in_use(tmp_path: Path) -> None:
    from cuiflow.interfaces.batch import RunInUse, RunLock

    run = _run_dir(tmp_path, [_result()])
    lock = RunLock(run)  # a batch session still writing to the run
    try:
        with pytest.raises(RunInUse):
            export_results(run, tmp_path / "m.sqlite")
        assert not (tmp_path / "m.sqlite").exists()
    finally:
        lock.release()
    export_results(run, tmp_path / "m.sqlite")


def test_iter_jsonl_skips_torn_last_line(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    w = open_writer(path)
    w.write(_result())
    w.close()
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"doc_id": "torn", "menti')
    assert [r.doc_id for r in iter_jsonl(path)] == ["notes/a.txt"]


def test_extract_output_by_suffix(
    tmp_path: Path, patch_extractor: object, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_extractor()  # type: ignore[operator]
    note = tmp_path / "n.txt"
    note.write_text("No chest pain.", encoding="utf-8")
    out = tmp_path / "o.sqlite"
    assert main(["extract", str(note), "-o", str(out)]) == 0
    assert sqlite3.connect(out).execute("SELECT COUNT(*) FROM documents").fetchone() == (1,)
    assert main(["extract", str(note), "-o", str(tmp_path / "o.csv")]) == 2  # OMOP via export
    assert "export" in capsys.readouterr().err


def test_datetime_is_formatted_for_omop(tmp_path: Path) -> None:
    from cuiflow.io.writers.omop import OmopNoteNlpWriter

    w = OmopNoteNlpWriter(
        tmp_path / "n.csv",
        note_ids=lambda _: 7,
        nlp_datetime=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    w.write(_result())
    w.close()
    row = next(csv.DictReader((tmp_path / "n.csv").open(encoding="utf-8", newline="")))
    assert (row["nlp_date"], row["nlp_datetime"]) == ("2026-01-02", "2026-01-02 03:04:05")


def test_note_nlp_id_schemes(tmp_path: Path) -> None:
    run = _run_dir(tmp_path, [_result("1")])
    hashed = tmp_path / "h.csv"
    export_results(run, hashed, note_ids=note_id_from_doc_id, id_scheme="hash63")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _load_note_nlp(hashed)  # 63-bit ids do not fit a stock v5.4 schema
    rows = list(csv.DictReader(hashed.open(encoding="utf-8", newline="")))
    again = tmp_path / "h2.csv"
    export_results(run, again, note_ids=note_id_from_doc_id, id_scheme="hash63")
    assert [r["note_nlp_id"] for r in rows] == [
        r["note_nlp_id"] for r in csv.DictReader(again.open(encoding="utf-8", newline=""))
    ]  # stable across exports

    offset = tmp_path / "o.csv"
    export_results(run, offset, note_ids=note_id_from_doc_id, first_id=500)
    assert [r[0] for r in _load_note_nlp(offset).execute("SELECT note_nlp_id FROM note_nlp")] == [
        500,
        501,
    ]
    with pytest.raises(ValueError, match="first_id"):
        export_results(run, tmp_path / "bad.csv", note_ids=note_id_from_doc_id, first_id=0)


def test_omop_prefers_a_code_that_maps_to_a_standard_concept(tmp_path: Path) -> None:
    from cuiflow.io.writers.omop import OmopNoteNlpWriter
    from cuiflow.terminology.omop import OmopConcepts

    # SNOMED is tried first but is known only as a non-standard, unmapped concept.
    table = {
        ("SNOMEDCT_US", "29857009"): OmopConcepts(111, 0),
        ("ICD10CM", "R07.9"): OmopConcepts(35211388, 77670),
    }

    class Vocab:
        def lookup(self, sab: str, code: str) -> OmopConcepts:
            return table.get((sab, code), OmopConcepts(0, 0))

    w = OmopNoteNlpWriter(
        tmp_path / "n.csv",
        note_ids=lambda d: 1,
        nlp_datetime=datetime(2026, 1, 1, tzinfo=UTC),
        vocabulary=Vocab(),  # type: ignore[arg-type]
    )
    chest, _ = _result().mentions
    assert w._concepts(chest) == (77670, 35211388)  # not SNOMED's (0, 111)
    del table[("ICD10CM", "R07.9")]
    assert w._concepts(chest) == (0, 111)  # nothing maps: the first code it knows
    w.close()


def test_note_id_from_doc_id_takes_ascii_digits_only() -> None:
    assert note_id_from_doc_id(" 42 ") == 42
    assert note_id_from_doc_id("²") is None  # isdigit() is True, but int() would raise
    assert note_id_from_doc_id("notes/1.txt") is None


def test_gz_suffix_is_case_insensitive(tmp_path: Path) -> None:
    import gzip

    path = tmp_path / "RESULTS.JSONL.GZ"
    w = open_writer(path)
    w.write(_result())
    w.close()
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        assert json.loads(fh.readline())["doc_id"] == "notes/a.txt"
    assert [r.doc_id for r in iter_jsonl(path)] == ["notes/a.txt"]


def test_omop_export_refuses_repeated_doc_ids(tmp_path: Path) -> None:
    from cuiflow.interfaces.export import load_note_ids

    run = _run_dir(tmp_path, [_result("1"), _result("2"), _result("1")])
    with pytest.raises(ValueError, match="document 3 repeats an earlier doc_id"):
        export_results(run, tmp_path / "n.csv", note_ids=note_id_from_doc_id)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["run"]  # no partial output left
    assert export_results(run, tmp_path / "m.sqlite")["documents"] == 3  # rows, not notes

    ids = tmp_path / "ids.csv"
    ids.write_text("doc_id,note_id\n1,10\n2,20\n1,10\n", encoding="utf-8")
    assert load_note_ids(ids) == {"1": 10, "2": 20}  # a repeated identical row is harmless
    ids.write_text("doc_id,note_id\n1,10\n1,11\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"ids\.csv:3: this doc_id already has a different"):
        load_note_ids(ids)


def test_omop_datetime_is_the_first_session_start(tmp_path: Path) -> None:
    run = _run_dir(tmp_path, [_result("1")])
    manifest = {
        "started_at": "2026-09-25T08:00:00Z",  # the resumed session
        "previous_sessions": [{"started_at": "2026-09-23T10:00:00Z"}],
        "config": {},
    }
    (run / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    out = tmp_path / "n.csv"
    export_results(run, out, note_ids=note_id_from_doc_id)
    row = next(csv.DictReader(out.open(encoding="utf-8", newline="")))
    assert row["nlp_datetime"] == "2026-09-23 10:00:00"


def test_term_modifiers_is_always_valid_json_within_the_column() -> None:
    from cuiflow.io.writers.omop import TERM_MODIFIERS_CHARS, term_modifiers

    (m, _) = _result().mentions
    worst = dataclasses.replace(
        m, cui="C" + "\x01" * 5000, assertions=AssertionStatus(subject="\x02" * 5000)
    )
    text = term_modifiers(worst)
    assert len(text) <= TERM_MODIFIERS_CHARS
    assert json.loads(text)["subject"] == "\x02" * 100
