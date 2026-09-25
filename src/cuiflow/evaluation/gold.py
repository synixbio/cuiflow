"""Build and check a human-annotated gold set.

Annotators fill a spreadsheet, one row per mention (the columns are ``WORKSHEET_COLUMNS``).
They type the mention's text as it appears in the note; the converter finds its character
offsets, so nobody counts characters by hand. :func:`convert_worksheet` turns that sheet plus
the notes into cuiflow reference JSONL; :func:`validate_records` checks any reference JSONL
against the guidelines' rules. Both report every problem with its row or record, not just the
first.

Standard library only.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GUIDELINES_VERSION = "1.0"

#: The semantic groups the guidelines put in scope (cTAKES' five major groups).
GROUPS: tuple[str, ...] = ("DISORDER", "FINDING", "DRUG", "PROCEDURE", "ANATOMY")
CUI_LESS = "CUI-LESS"
_CUI = re.compile(r"^C\d{7}$")

#: Attributes an annotator must judge on every mention; ``generic`` is optional.
REQUIRED = ("negated", "subject", "history_of", "uncertain", "conditional")
OPTIONAL = ("generic",)
BOOLEAN = ("negated", "history_of", "uncertain", "conditional", "generic")
#: Sources whose every mention must have the required attributes judged.
JUDGED_SOURCES = ("human:", "adjudicated:")
SUBJECTS = ("patient", "family_member", "other")

WORKSHEET_COLUMNS = (
    "doc_id",
    "mention_text",
    "occurrence",
    "start",
    "end",
    "group",
    "cui",
    "negated",
    "subject",
    "history_of",
    "uncertain",
    "conditional",
    "generic",
    "annotator_note",
)

_TRUE = {"yes", "y", "true", "t", "1"}
_FALSE = {"no", "n", "false", "f", "0"}


@dataclass
class Problems:
    """Everything wrong with an input, each tied to where it is."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.errors)


def _bool(value: str) -> bool | None:
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    if v == "":
        return None
    raise ValueError(f"expected yes/no, got {value!r}")


def _read_note(path: Path) -> str:
    # Verbatim, line endings untranslated, so offsets index the file exactly.
    with path.open(encoding="utf-8", newline="") as fh:
        return fh.read()


def _find(text: str, needle: str, occurrence: int) -> int | None:
    """Start of the ``occurrence``-th (1-based) exact match of ``needle``, or None."""
    pos = -1
    for _ in range(occurrence):
        pos = text.find(needle, pos + 1)
        if pos < 0:
            return None
    return pos


def _row_to_mention(
    row: Mapping[str, str], text: str, where: str, problems: Problems
) -> dict[str, Any] | None:
    needle = row.get("mention_text", "")
    if not needle.strip():
        problems.errors.append(f"{where}: mention_text is empty")
        return None
    if needle != needle.strip():
        problems.errors.append(f"{where}: mention_text has leading/trailing spaces")
        return None

    start_raw, end_raw = row.get("start", "").strip(), row.get("end", "").strip()
    if start_raw or end_raw:
        try:
            start, end = int(start_raw), int(end_raw)
        except ValueError:
            problems.errors.append(f"{where}: start/end must both be integers")
            return None
        if text[start:end] != needle:
            problems.errors.append(
                f"{where}: text at [{start}:{end}] is {text[start:end]!r}, not {needle!r}"
            )
            return None
    else:
        try:
            occurrence = int(row.get("occurrence", "").strip() or 1)
        except ValueError:
            problems.errors.append(f"{where}: occurrence must be a whole number")
            return None
        found = _find(text, needle, occurrence)
        if found is None:
            count = text.count(needle)
            problems.errors.append(
                f"{where}: {needle!r} occurs {count} time(s) in the note; occurrence "
                f"{occurrence} does not exist (matching is exact and case-sensitive)"
            )
            return None
        start, end = found, found + len(needle)

    group = row.get("group", "").strip().upper()
    cui = row.get("cui", "").strip().upper()
    assertions: dict[str, Any] = {}
    for attr in (*REQUIRED, *OPTIONAL):
        raw = row.get(attr, "")
        if attr == "subject":
            value: Any = raw.strip().lower().replace(" ", "_") or None
        else:
            try:
                value = _bool(raw)
            except ValueError as exc:
                problems.errors.append(f"{where}: {attr}: {exc}")
                return None
        assertions[attr] = value
    mention: dict[str, Any] = {
        "start": start,
        "end": end,
        "text": needle,
        "cui": cui,
        "group": group,
        "assertions": assertions,
    }
    note = row.get("annotator_note", "").strip()
    if note:
        mention["note"] = note
    return mention


def convert_worksheet(
    worksheet: Path,
    notes_dir: Path,
    *,
    annotator: str,
    guidelines_version: str = GUIDELINES_VERSION,
    source: str | None = None,
) -> tuple[list[dict[str, Any]], Problems]:
    """Worksheet rows + note files -> reference records, and every problem found.

    ``source`` defaults to ``human:<annotator>``. An adjudicated set says who adjudicated it,
    e.g. ``adjudicated:claude-draft``, so a draft is never mistaken for human annotation.

    ``doc_id`` in the sheet is the note's file name in ``notes_dir``. Records are returned even
    when there are problems, so a caller can report them all; do not write records out while
    ``problems.errors`` is non-empty.
    """
    problems = Problems()
    with worksheet.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [
            c for c in ("doc_id", "mention_text", "cui") if c not in (reader.fieldnames or ())
        ]
        if missing:
            problems.errors.append(f"{worksheet.name}: missing column(s): {', '.join(missing)}")
            return [], problems
        rows = list(reader)

    texts: dict[str, str] = {}
    by_doc: dict[str, list[dict[str, Any]]] = {}
    for n, row in enumerate(rows, start=2):  # row 1 is the header
        doc_id = (row.get("doc_id") or "").strip()
        where = f"row {n}"
        if not doc_id:
            if any((v or "").strip() for v in row.values()):
                problems.errors.append(f"{where}: doc_id is empty")
            continue
        if doc_id not in texts:
            path = notes_dir / doc_id
            if not path.is_file():
                problems.errors.append(f"{where}: note file not found: {path}")
                texts[doc_id] = ""
                continue
            texts[doc_id] = _read_note(path)
        if not texts[doc_id]:
            continue
        mention = _row_to_mention(row, texts[doc_id], f"{where} ({doc_id})", problems)
        if mention is not None:
            by_doc.setdefault(doc_id, []).append(mention)

    # Notes with no rows are real gold data too ("nothing in scope here"), but a missing row
    # is also the commonest mistake, so they are listed as warnings.
    for path in sorted(notes_dir.glob("*.txt")):
        if path.name not in texts:
            problems.warnings.append(f"{path.name}: no rows in the worksheet (kept, no mentions)")
            texts[path.name] = _read_note(path)

    records: list[dict[str, Any]] = [
        {
            "doc_id": doc_id,
            "text": texts[doc_id],
            "source": source or f"human:{annotator}",
            "guidelines": guidelines_version,
            "mentions": sorted(by_doc.get(doc_id, []), key=lambda m: (m["start"], m["end"])),
        }
        for doc_id in sorted(texts)
        if texts[doc_id]
    ]
    # Row-level checks above cover parsing; record-level rules (CUI format, required
    # attributes, duplicates) are shared with validate_records.
    checked = validate_records(records)
    problems.errors.extend(checked.errors)
    problems.warnings.extend(checked.warnings)
    return records, problems


def validate_records(records: Iterable[Mapping[str, Any]]) -> Problems:
    """Check reference records against the guidelines' rules."""
    problems = Problems()
    seen_docs: set[str] = set()
    for i, rec in enumerate(records, start=1):
        doc_id = rec.get("doc_id")
        text = rec.get("text")
        where = f"record {i} ({doc_id})"
        if not isinstance(doc_id, str) or not doc_id:
            problems.errors.append(f"record {i}: doc_id missing")
            continue
        if doc_id in seen_docs:
            problems.errors.append(f"{where}: duplicate doc_id")
        seen_docs.add(doc_id)
        if not isinstance(text, str):
            problems.errors.append(f"{where}: text missing")
            continue
        seen_spans: set[tuple[int, int, str]] = set()
        for j, m in enumerate(rec.get("mentions", ()), start=1):
            mw = f"{where} mention {j}"
            start, end = m.get("start"), m.get("end")
            if not (
                isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text)
            ):
                problems.errors.append(f"{mw}: span ({start}, {end}) is empty or outside the text")
                continue
            span_text = text[start:end]
            if "text" in m and m["text"] != span_text:
                problems.errors.append(
                    f"{mw}: text {m['text']!r} != note[{start}:{end}] {span_text!r}"
                )
            if span_text != span_text.strip():
                problems.errors.append(f"{mw}: span starts or ends with whitespace")
            cui = m.get("cui", "")
            if cui == CUI_LESS:
                problems.warnings.append(f"{mw}: {span_text!r} is CUI-LESS (scored as a miss)")
            elif not _CUI.match(cui or ""):
                problems.errors.append(f"{mw}: cui {cui!r} is not C + 7 digits or {CUI_LESS}")
            group = m.get("group")
            if group is not None and group not in GROUPS:
                problems.errors.append(f"{mw}: group {group!r} is not one of {', '.join(GROUPS)}")
            if group is None and not m.get("tuis"):
                problems.errors.append(f"{mw}: needs a group (or tuis) to be scored")
            key = (start, end, cui)
            if key in seen_spans:
                problems.errors.append(f"{mw}: duplicate of an earlier mention")
            seen_spans.add(key)
            a = m.get("assertions", {})
            if rec.get("source", "").startswith(JUDGED_SOURCES):
                for attr in REQUIRED:
                    if a.get(attr) is None:
                        problems.errors.append(f"{mw}: {attr} not judged (required)")
            for attr in BOOLEAN:
                if a.get(attr) not in (None, True, False):
                    problems.errors.append(f"{mw}: {attr} must be true, false or null")
            if a.get("subject") not in (None, *SUBJECTS):
                problems.errors.append(f"{mw}: subject must be one of {', '.join(SUBJECTS)}")
    return problems


def write_jsonl(records: Iterable[Mapping[str, Any]], path: Path) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as fh:  # never overwrite a gold file
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
