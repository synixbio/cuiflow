from __future__ import annotations

import codecs
import json
from pathlib import Path

import pytest

from cuiflow.core.models import AssertionStatus, DocumentResult, Mention
from cuiflow.io.readers import iter_documents
from cuiflow.io.writers import open_writer
from cuiflow.io.writers.jsonl import read_jsonl
from cuiflow.io.writers.omop import note_nlp_id, term_exists, term_temporal


def test_reader_preserves_crlf_offsets(tmp_path: Path) -> None:
    p = tmp_path / "n.txt"
    p.write_bytes(b"line one\r\nchest pain\r\n")
    (doc,) = iter_documents([p])
    assert doc.text.index("chest") == 10  # \r\n kept, so offsets index the file verbatim


def test_reader_walks_folders_and_jsonl(tmp_path: Path) -> None:
    (tmp_path / "d" / "sub").mkdir(parents=True)
    (tmp_path / "d" / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "d" / "sub" / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "d" / "skip.csv").write_text("x", encoding="utf-8")
    j = tmp_path / "in.jsonl"
    j.write_text(json.dumps({"id": "n7", "text": "t"}) + "\n\n", encoding="utf-8")
    docs = list(iter_documents([tmp_path / "d", j]))
    assert [Path(d.doc_id).name for d in docs[:2]] == ["b.txt", "a.txt"]
    assert docs[2].doc_id == "n7"
    with pytest.raises(FileNotFoundError):
        list(iter_documents([tmp_path / "missing.txt"]))


@pytest.mark.parametrize("name", ["out.jsonl", "out.jsonl.gz"])
def test_jsonl_writer_round_trip_and_no_overwrite(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    w = open_writer(path)
    w.write(DocumentResult(doc_id="d", mentions=()))
    w.close()
    assert read_jsonl(path)[0].doc_id == "d"
    with pytest.raises(FileExistsError):
        open_writer(path)


def _m(**a: object) -> Mention:
    return Mention("id", 0, 1, "x", "C1", "X", "FINDING", AssertionStatus(**a))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("assertions", "expected"),
    [
        ({"negated": False, "subject": "patient"}, "Y"),
        ({"negated": True}, "N"),
        ({"negated": False, "subject": "family_member"}, "N"),
        ({"negated": False, "uncertain": True}, "N"),
        ({"negated": False, "conditional": True}, "N"),
        ({}, None),  # negation not assessed: NULL, never a guess
        ({"subject": "patient"}, None),
    ],
)
def test_omop_term_exists(assertions: dict[str, object], expected: str | None) -> None:
    assert term_exists(_m(**assertions)) == expected


def test_omop_term_temporal() -> None:
    assert term_temporal(_m(history_of=True)) == "past"
    assert term_temporal(_m(history_of=False)) == "present"
    assert term_temporal(_m(temporality="historical")) == "past"
    assert term_temporal(_m()) is None


def test_note_nlp_id_is_stable_positive_int64() -> None:
    a = note_nlp_id("doc", 1, 5, "C1")
    assert a == note_nlp_id("doc", 1, 5, "C1") and 0 <= a < 2**63
    assert a != note_nlp_id("doc", 1, 5, "C2")


def test_reader_keeps_falsy_ids_and_reads_gzipped_jsonl(tmp_path: Path) -> None:
    import gzip

    records = [{"id": 0, "text": "a"}, {"id": "", "doc_id": "x", "text": "b"}, {"text": "c"}]
    body = "".join(json.dumps(r) + "\n" for r in records)
    plain, packed = tmp_path / "in.jsonl", tmp_path / "in.JSONL.gz"
    plain.write_text(body, encoding="utf-8")
    with gzip.open(packed, "wt", encoding="utf-8") as fh:
        fh.write(body)
    assert [d.doc_id for d in iter_documents([plain])] == ["0", "x", "in.jsonl:3"]
    assert [(d.doc_id, d.text) for d in iter_documents([packed])] == [
        ("0", "a"),
        ("x", "b"),
        ("in.JSONL.gz:3", "c"),
    ]


def test_text_files_drop_the_bom_and_count_replaced_bytes(tmp_path: Path) -> None:
    from cuiflow.io.readers import decode_utf8, iter_documents

    (tmp_path / "bom.txt").write_bytes(b"\xef\xbb\xbfHPI: chest pain")
    (tmp_path / "bad.txt").write_bytes(b"caf\xe9 and \xff\xfe ok \xe2\x82")
    docs = {Path(d.doc_id).name: d for d in iter_documents([tmp_path])}
    assert docs["bom.txt"].text == "HPI: chest pain" and docs["bom.txt"].replaced == 0
    raw = (tmp_path / "bad.txt").read_bytes()
    assert docs["bad.txt"].text == raw.decode("utf-8", errors="replace")  # same text as before
    assert docs["bad.txt"].replaced == raw.decode("utf-8", errors="replace").count("�") == 4
    assert decode_utf8("naïve".encode()) == ("naïve", 0)


def test_unreadable_jsonl_records_are_reported_by_place_and_type(tmp_path: Path) -> None:
    from cuiflow.io.readers import InputError

    j = tmp_path / "in.jsonl"
    lines = [
        codecs.BOM_UTF8 + json.dumps({"id": "a", "text": "ok"}).encode(),
        b'{"id": "b", "text": "SECRET',  # truncated
        json.dumps({"id": "c"}).encode(),  # no text
        json.dumps(["not", "an", "object"]).encode(),
        json.dumps({"id": "e", "text": "caf"}).encode()[:-2] + b'\xe9"}',
    ]
    j.write_bytes(b"\n".join(lines) + b"\n")
    seen: list[tuple[str, str]] = []
    docs = list(iter_documents([j], on_error=lambda where, kind: seen.append((where, kind))))
    assert [(d.doc_id, d.replaced) for d in docs] == [("a", 0), ("e", 1)]
    assert seen == [
        (f"{j}:2", "JSONDecodeError"),
        (f"{j}:3", "KeyError"),
        (f"{j}:4", "TypeError"),
    ]
    with pytest.raises(InputError, match=r"in\.jsonl:2: JSONDecodeError") as info:
        list(iter_documents([j]))  # without on_error the first bad record stops the read
    assert "SECRET" not in str(info.value)


def test_folders_read_text_files_and_list_the_rest(tmp_path: Path) -> None:
    from cuiflow.io.readers import input_files, skipped_in_folders

    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "out.jsonl").write_text(json.dumps({"text": "x"}) + "\n", encoding="utf-8")
    assert [Path(d.doc_id).name for d in iter_documents([tmp_path])] == ["a.txt"]
    assert [p.name for p in input_files([tmp_path])] == ["a.txt"]
    assert [p.name for p in skipped_in_folders([tmp_path, "-"])] == ["out.jsonl"]
