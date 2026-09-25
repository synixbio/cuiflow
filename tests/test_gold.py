"""The gold-set worksheet converter and validator."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from cuiflow.evaluation.gold import (
    WORKSHEET_COLUMNS,
    convert_worksheet,
    validate_records,
    write_jsonl,
)

NOTE = "No chest pain.\nChest pain again; chest pain."


def _row(**kw: str) -> dict[str, str]:
    row = dict.fromkeys(WORKSHEET_COLUMNS, "")
    row.update(
        doc_id="n.txt",
        group="FINDING",
        cui="C0008031",
        negated="no",
        subject="patient",
        history_of="no",
        uncertain="no",
        conditional="no",
    )
    row.update(kw)
    return row


def _sheet(tmp_path: Path, rows: list[dict[str, str]], note: str = NOTE) -> tuple[Path, Path]:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "n.txt").write_bytes(note.encode("utf-8"))
    sheet = tmp_path / "mentions.csv"
    with sheet.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=WORKSHEET_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return sheet, notes


def test_offsets_found_from_text_and_occurrence(tmp_path: Path) -> None:
    sheet, notes = _sheet(
        tmp_path,
        [
            _row(mention_text="chest pain", negated="yes"),
            _row(mention_text="chest pain", occurrence="2"),
            _row(mention_text="Chest pain", generic="", annotator_note="case matters"),
        ],
    )
    records, problems = convert_worksheet(sheet, notes, annotator="A1")
    assert not problems, problems.errors
    (rec,) = records
    assert rec["source"] == "human:A1" and rec["guidelines"] == "1.0"
    spans = [(m["start"], m["end"], m["assertions"]["negated"]) for m in rec["mentions"]]
    assert spans == [(3, 13, True), (15, 25, False), (33, 43, False)]
    assert all(NOTE[s:e].lower() == "chest pain" for s, e, _ in spans)
    assert rec["mentions"][1]["assertions"]["generic"] is None  # blank = not judged
    assert rec["mentions"][1]["note"] == "case matters"


def test_crlf_note_offsets_index_the_file(tmp_path: Path) -> None:
    sheet, notes = _sheet(tmp_path, [_row(mention_text="pain")], note="line\r\npain")
    (rec,), problems = convert_worksheet(sheet, notes, annotator="A1")
    assert not problems and rec["mentions"][0]["start"] == 6


def test_every_problem_is_reported_with_its_row(tmp_path: Path) -> None:
    sheet, notes = _sheet(
        tmp_path,
        [
            _row(mention_text="chest pain", occurrence="3"),  # row 2: only 2 exist
            _row(mention_text="fever"),  # row 3: not in the note
            _row(mention_text="chest pain", negated="maybe"),  # row 4
            _row(mention_text="chest pain", cui="C123"),  # row 5
            _row(mention_text="chest pain", group="SYMPTOM"),  # row 6
            _row(mention_text="chest pain", history_of=""),  # row 7: required, blank
            _row(mention_text="chest pain", start="3", end="12"),  # row 8: wrong offsets
            _row(doc_id="missing.txt", mention_text="x"),  # row 9
        ],
    )
    _, problems = convert_worksheet(sheet, notes, annotator="A1")
    text = "\n".join(problems.errors)
    for expected in (
        "row 2",
        "occurs 2 time(s)",
        "row 3",
        "row 4",
        "negated",
        "C123",
        "SYMPTOM",
        "history_of not judged",
        "row 8",
        "note file not found",
    ):
        assert expected in text, expected


def test_duplicates_and_forgotten_notes(tmp_path: Path) -> None:
    sheet, notes = _sheet(
        tmp_path, [_row(mention_text="chest pain"), _row(mention_text="chest pain")]
    )
    (notes / "other.txt").write_text("nothing here", encoding="utf-8")
    records, problems = convert_worksheet(sheet, notes, annotator="A1")
    assert any("duplicate" in e for e in problems.errors)
    assert any("other.txt: no rows" in w for w in problems.warnings)
    assert {r["doc_id"] for r in records} == {"n.txt", "other.txt"}


def test_validate_catches_hand_edited_records() -> None:
    rec = {
        "doc_id": "d",
        "text": "chest pain",
        "source": "human:A1",
        "mentions": [
            {"start": 0, "end": 5, "text": "chest pain", "cui": "C0008031", "group": "FINDING",
             "assertions": {"negated": False, "subject": "someone", "history_of": False,
                            "uncertain": False, "conditional": False}},
            {"start": 0, "end": 10, "cui": "CUI-LESS", "assertions": {}},
        ],
    }  # fmt: skip
    problems = validate_records([rec])
    text = "\n".join(problems.errors)
    assert "!= note[0:5]" in text and "subject must be" in text
    assert "needs a group" in text and "negated not judged" in text
    assert any("CUI-LESS" in w for w in problems.warnings)


def test_write_never_overwrites(tmp_path: Path) -> None:
    out = tmp_path / "gold.jsonl"
    write_jsonl([{"doc_id": "d"}], out)
    assert json.loads(out.read_text(encoding="utf-8")) == {"doc_id": "d"}
    with pytest.raises(FileExistsError):
        write_jsonl([], out)


def test_convert_records_an_adjudicated_source(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "n1.txt").write_text("Denies chest pain.", encoding="utf-8")
    sheet = tmp_path / "sheet.csv"
    sheet.write_text(
        "doc_id,mention_text,group,cui,negated,subject,history_of,uncertain,conditional\n"
        "n1.txt,chest pain,FINDING,C0008031,yes,patient,no,no,no\n",
        encoding="utf-8",
    )
    records, problems = convert_worksheet(
        sheet, notes, annotator="x", guidelines_version="1.1", source="adjudicated:claude-draft"
    )
    assert not problems.errors
    (rec,) = records
    assert rec["source"] == "adjudicated:claude-draft" and rec["guidelines"] == "1.1"

    del rec["mentions"][0]["assertions"]["negated"]  # adjudicated sets must judge every attribute
    assert any("negated not judged" in e for e in validate_records(records).errors)
