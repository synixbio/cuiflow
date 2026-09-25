"""Scripts in tools/ that need no UMLS data."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_notes_writes_each_text_verbatim(tmp_path: Path) -> None:
    export_notes = _load("export_notes").export_notes
    texts = {"a.txt": "Line one.\r\nNo chest pain. é\n", "b.txt": ""}
    ref = tmp_path / "gold.jsonl"
    ref.write_text(
        "".join(
            json.dumps({"doc_id": k, "text": v, "mentions": []}) + "\n" for k, v in texts.items()
        )
        + "\n",
        encoding="utf-8",
    )
    written = export_notes(ref, tmp_path / "notes")
    assert sorted(p.name for p in written) == ["a.txt", "b.txt"]
    for name, text in texts.items():
        assert (tmp_path / "notes" / name).read_bytes() == text.encode("utf-8")
    with pytest.raises(FileExistsError):  # never overwrites
        export_notes(ref, tmp_path / "notes")


def test_export_notes_refuses_a_doc_id_that_is_a_path(tmp_path: Path) -> None:
    ref = tmp_path / "gold.jsonl"
    ref.write_text(json.dumps({"doc_id": "../x.txt", "text": "t"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="plain file name"):
        _load("export_notes").export_notes(ref, tmp_path / "notes")
    assert not (tmp_path / "x.txt").exists()


def test_carry_gold_moves_offsets_and_stops_on_changed_mentions() -> None:
    carry = _load("carry_gold").carry
    old = "- No chest pain.\n- Nasal discharge today.\n- Diabetes."

    def mention(text: str, cui: str) -> dict[str, object]:
        start = old.index(text)
        return {"start": start, "end": start + len(text), "text": text, "cui": cui, "note": "n"}

    record = {
        "doc_id": "a.txt",
        "text": old,
        "source": "s",
        "mentions": [
            mention("chest pain", "C1"),
            mention("Nasal discharge", "C2"),
            mention("Diabetes", "C3"),
        ],
    }
    new = "* No chest pain.\n* Nasal drainage today.\n* Diabetes."
    carried, unreviewed = carry(record, "b.txt", new, {})
    assert [m["text"] for m in carried["mentions"]] == ["chest pain", "Nasal drainage", "Diabetes"]
    assert all(new[m["start"] : m["end"]] == m["text"] for m in carried["mentions"])
    assert carried["doc_id"] == "b.txt" and carried["source"] == "s" and carried["text"] == new
    start = new.index("Nasal")
    assert unreviewed == [f"b.txt:{start}: 'Nasal discharge' -> 'Nasal drainage'"]

    carried, unreviewed = carry(record, "b.txt", new, {f"b.txt:{start}": "same concept"})
    assert unreviewed == []
    assert carried["mentions"][1]["note"].endswith("reviewed: same concept")
    assert carried["mentions"][0]["note"] == "n"  # untouched mentions keep their note

    _, unreviewed = carry(record, "b.txt", new.replace("chest pain", "chest ache"), {})
    assert any("lies on an edit" in u for u in unreviewed)  # the end moved into an edit
