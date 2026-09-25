"""Merge two annotations of the same notes into one adjudicated reference file.

Mentions both annotators marked identically (span, CUI and every attribute) are kept. Every
other mention needs a row in the decisions CSV: ``take`` is ``first``, ``second`` or ``drop``,
and ``overrides`` (``cui=C0000001;negated=yes``) changes the taken mention. A disagreement
without a decision, a decision for a mention that does not exist, and overlapping kept spans
are errors.

    python tools/adjudicate_pair.py first.jsonl second.jsonl decisions.csv --out gold.jsonl `
        --source adjudicated:<who> --guidelines-version 1.2
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import sys
from pathlib import Path

VALUES = {"yes": True, "no": False}


def _load(path: Path) -> tuple[dict[str, str], dict[tuple[str, int, int], dict]]:
    texts, mentions = {}, {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rec = json.loads(line)
                texts[rec["doc_id"]] = rec["text"]
                for m in rec["mentions"]:
                    mentions[(rec["doc_id"], m["start"], m["end"])] = m
    return texts, mentions


def _same(a: dict, b: dict) -> bool:
    return a["cui"] == b["cui"] and a["assertions"] == b["assertions"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("first", type=Path)
    ap.add_argument("second", type=Path)
    ap.add_argument("decisions", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--guidelines-version", required=True)
    args = ap.parse_args()

    texts, first = _load(args.first)
    _, second = _load(args.second)
    with args.decisions.open(encoding="utf-8", newline="") as fh:
        decisions = {(d["doc_id"], int(d["start"]), int(d["end"])): d for d in csv.DictReader(fh)}

    errors, kept = [], {}
    for key in sorted(first.keys() | second.keys()):
        a, b = first.get(key), second.get(key)
        d = decisions.pop(key, None)
        if d is None:
            if a and b and _same(a, b):
                kept[key] = dict(b, note=b.get("note") or a.get("note", ""))
            else:
                errors.append(f"{key}: {texts[key[0]][key[1] : key[2]]!r} disagrees, no decision")
            continue
        if d["take"] == "drop":
            continue
        m = {"first": a, "second": b}.get(d["take"])
        if m is None:
            errors.append(f"{key}: take={d['take']!r} but that annotation has no such mention")
            continue
        m = json.loads(json.dumps(m))
        for item in filter(None, d["overrides"].split(";")):
            field, value = item.split("=", 1)
            if field == "cui":
                m["cui"] = value
            else:
                m["assertions"][field] = VALUES.get(value, value)
        m["note"] = f"adjudicated ({d['rule']}): {d['rationale']}"
        kept[key] = m
    errors += [f"{k}: decision for a mention neither annotation has" for k in decisions]

    by_doc: dict[str, list[dict]] = {doc: [] for doc in texts}
    for (doc, _, _), m in sorted(kept.items()):
        by_doc[doc].append(m)
    for doc, ms in by_doc.items():
        for x, y in itertools.pairwise(ms):
            if y["start"] < x["end"]:
                errors.append(f"{doc}: {x['text']!r} overlaps {y['text']!r}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1

    with args.out.open("w", encoding="utf-8", newline="\n") as fh:
        for doc in sorted(by_doc):
            rec = {
                "doc_id": doc,
                "text": texts[doc],
                "source": args.source,
                "guidelines": args.guidelines_version,
                "mentions": by_doc[doc],
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"{args.out}: {len(by_doc)} notes, {len(kept)} mentions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
