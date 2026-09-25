"""Carry a reference file's labels over to edited copies of its notes.

    python tools/carry_gold.py <gold.jsonl> --notes synthetic `
        --rename "doc_(\\d+)\\.txt=doc_0\\1.txt" --only 1-20 --out bridge_gold.jsonl

For each reference record whose (renamed) note exists in ``--notes``, the old and new texts are
aligned character by character and every mention's offsets are moved with the text. A mention
whose span text changes, or whose start or end falls inside an edit, is kept with a note saying
what changed and is listed on stderr for review; ``--review`` names mentions (``doc:start``,
new offsets) that were reviewed and may keep their labels, with the reason appended to the
note. Unreviewed changes make the script fail, so nothing is carried over silently.
Every other field (``source``, ``guidelines``, the labels) is copied unchanged.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any


def offset_map(old: str, new: str) -> tuple[dict[int, int], list[tuple[int, int]]]:
    """Old offset -> new offset for every character kept unchanged, and the old edited ranges."""
    kept: dict[int, int] = {}
    edits = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for op, i1, i2, j1, _ in matcher.get_opcodes():
        if op == "equal":
            kept.update((i1 + k, j1 + k) for k in range(i2 - i1))
        else:
            edits.append((i1, i2))
    return kept, edits


def carry(
    record: dict[str, Any], doc_id: str, new: str, reviewed: dict[str, str]
) -> tuple[dict[str, Any], list[str]]:
    old = record["text"]
    kept, edits = offset_map(old, new)
    mentions, unreviewed = [], []
    for m in record["mentions"]:
        start = kept.get(m["start"])
        last = kept.get(m["end"] - 1)
        if start is None or last is None:
            unreviewed.append(f"{doc_id}: {m['text']!r} at {m['start']} lies on an edit")
            continue
        moved = {**m, "start": start, "end": last + 1, "text": new[start : last + 1]}
        touched = any(m["start"] < i2 and i1 < m["end"] for i1, i2 in edits)
        if touched or moved["text"] != m["text"]:
            key = f"{doc_id}:{start}"
            change = f"carried over: span text was {m['text']!r}"
            if key in reviewed:
                change += f"; reviewed: {reviewed[key]}"
            else:
                unreviewed.append(f"{key}: {m['text']!r} -> {moved['text']!r}")
            moved["note"] = f"{m['note']}; {change}" if m.get("note") else change
        mentions.append(moved)
    return {**record, "doc_id": doc_id, "text": new, "mentions": mentions}, unreviewed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("reference", type=Path, help="reference JSONL holding the original notes")
    ap.add_argument("--notes", type=Path, required=True, help="folder of edited .txt notes")
    ap.add_argument("--rename", help="PATTERN=REPLACEMENT (re.sub) from old doc_id to file name")
    ap.add_argument("--only", help="range of record numbers to carry, 1-based: e.g. 1-20")
    ap.add_argument("--review", action="append", default=[], help="DOC:START=reason, repeatable")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    pattern, _, replacement = (args.rename or "(.*)=\\1").partition("=")
    first, _, last = (args.only or "1-999999999").partition("-")
    reviewed = dict(r.split("=", 1) for r in args.review)
    records, problems = [], []
    with args.reference.open(encoding="utf-8") as fh:
        lines = [line for line in fh if line.strip()]
    for n, line in enumerate(lines, 1):
        if not int(first) <= n <= int(last or first):
            continue
        record = json.loads(line)
        doc_id = re.sub(pattern, replacement, record["doc_id"])
        path = args.notes / doc_id
        if not path.is_file():
            problems.append(f"{doc_id}: not in {args.notes}")
            continue
        with path.open(encoding="utf-8", newline="") as note:  # offsets index the file verbatim
            carried, unreviewed = carry(record, doc_id, note.read(), reviewed)
        records.append(carried)
        problems += unreviewed
    unused = set(reviewed) - {f"{r['doc_id']}:{m['start']}" for r in records for m in r["mentions"]}
    problems += [f"--review {key} names no mention" for key in sorted(unused)]
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8", newline="\n") as fh:
        fh.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    total = sum(len(r["mentions"]) for r in records)
    print(f"{args.out}: {len(records)} notes, {total} mentions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
