"""Apply adjudication decisions to the annotators' agreement table.

    python tools/adjudicate.py --spans <annotation>/agreement/spans.csv \
        --decisions <annotation>/adjudicated/decisions.csv \
        --global-rules <annotation>/adjudicated/global_rules.csv \
        --meta <UMLS 2026AA>/META --notes <gold.jsonl> --out <annotation>/adjudicated

Inputs: ``agreement/spans.csv`` (one row per span any annotator marked) and a decisions file
with one row per span that was not unanimous: ``idx`` (the spans.csv data row, 0-based),
``action`` (``p1``/``p2``/``p3`` = take that annotator's labels, or ``drop``), ``overrides``
(``key=value;...`` for cui, group, an attribute, or ``span=start-end``) and ``rationale`` (the
guideline rule). Unanimous spans are kept as marked unless a decision names them, which is how a
later correction is recorded. Then, for every kept mention:

- CUIs are rewritten by ``global_rules.csv`` (``from_cui,to_cui,rationale``);
- the group is set from the concept's semantic types with cTAKES' grouping (the engines' rule),
  keeping the annotator's group only when the types map to none of the five;
- every CUI must exist in the given UMLS release (MRCONSO), and no two kept mentions may
  overlap (the longest-span rule), or the script fails.

Writes ``mentions.csv`` (a worksheet for ``cuiflow gold convert``, with the rationale as the
note) and ``spans_adjudicated.csv`` (spans.csv with the adjudication column filled).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from itertools import pairwise
from pathlib import Path

ANNOTATORS = ("person_1", "person_2", "person_3")
ATTRS = ("negated", "subject", "history_of", "uncertain", "conditional", "generic")
FIVE = ("DISORDER", "FINDING", "DRUG", "PROCEDURE", "ANATOMY")
PICK = {"p1": "person_1", "p2": "person_2", "p3": "person_3"}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _labels(row: dict[str, str], who: str) -> dict[str, str]:
    return {
        "cui": row[f"{who}_cui"],
        "group": row[f"{who}_group"],
        **{a: row[f"{who}_{a}"] for a in ATTRS},
    }


def _umls(meta: Path, cuis: set[str]) -> tuple[set[str], dict[str, list[str]]]:
    found: set[str] = set()
    with (meta / "MRCONSO.RRF").open(encoding="utf-8") as fh:
        for line in fh:
            if line[:8] in cuis:
                found.add(line[:8])
    tuis: dict[str, list[str]] = {}
    with (meta / "MRSTY.RRF").open(encoding="utf-8") as fh:
        for line in fh:
            p = line.split("|")
            if p[0] in cuis:
                tuis.setdefault(p[0], []).append(p[1])
    return found, tuis


def adjudicate(
    spans: list[dict[str, str]],
    decisions: dict[int, dict[str, str]],
    rules: dict[str, dict[str, str]],
) -> tuple[list[dict[str, str]], list[str]]:
    """Kept mentions (before grouping) and every problem; fills ``adjudication`` in ``spans``."""
    errors: list[str] = []
    kept: list[dict[str, str]] = []
    for idx, row in enumerate(spans):
        # A decision on a unanimous span is a later correction (e.g. a guidelines revision).
        if row["status"] == "agree" and idx not in decisions:
            who = next(p for p in ANNOTATORS if row[f"{p}_cui"])
            labels, rationale = _labels(row, who), "unanimous"
            row["adjudication"] = "keep: unanimous"
        else:
            d = decisions.get(idx)
            if d is None:
                errors.append(f"span {idx} ({row['doc_id']} {row['text']!r}) has no decision")
                continue
            rationale = d["rationale"]
            if d["action"] == "drop":
                row["adjudication"] = f"drop: {rationale}"
                continue
            who = PICK[d["action"]]
            if not row[f"{who}_cui"]:
                errors.append(f"span {idx}: {who} did not mark it")
                continue
            labels = _labels(row, who)
            for kv in filter(None, d["overrides"].split(";")):
                key, value = kv.split("=", 1)
                if key == "span":
                    row["start"], row["end"] = value.split("-")
                else:
                    labels[key] = value
            what = ", ".join(filter(None, [d["action"], d["overrides"]]))
            row["adjudication"] = f"keep ({what}): {rationale}"
        mention = {"doc_id": row["doc_id"], "start": row["start"], "end": row["end"], **labels}
        rule = rules.get(mention["cui"])
        if rule:
            mention["cui"] = rule["to_cui"]
            rationale += f"; global rule: {rule['rationale']}"
        mention["annotator_note"] = rationale
        kept.append(mention)
    return kept, errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--spans", type=Path, required=True)
    ap.add_argument("--decisions", type=Path, required=True)
    ap.add_argument("--global-rules", type=Path)
    ap.add_argument(
        "--notes",
        type=Path,
        required=True,
        help="folder of .txt notes, or a reference JSONL holding them (e.g. the gold set)",
    )
    ap.add_argument("--meta", type=Path, required=True, help="UMLS META folder (MRCONSO, MRSTY)")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    from umlsmatch.umls.semantic_tui import best_group, group_for_tui

    spans = _read_csv(args.spans)
    decisions = {int(d["idx"]): d for d in _read_csv(args.decisions)}
    rules = {r["from_cui"]: r for r in _read_csv(args.global_rules)} if args.global_rules else {}
    kept, errors = adjudicate(spans, decisions, rules)

    cuis = {m["cui"] for m in kept if m["cui"] != "CUI-LESS"}
    found, tuis = _umls(args.meta, cuis)
    errors += [f"{c} is not in this UMLS release" for c in sorted(cuis - found)]
    regrouped = 0
    for m in kept:
        if m["cui"] == "CUI-LESS":
            continue
        group = best_group({group_for_tui(t) for t in tuis.get(m["cui"], [])}).name
        if group in FIVE and group != m["group"]:
            m["group"] = group
            regrouped += 1

    texts: dict[str, str] = {}
    if args.notes.is_file():  # a reference JSONL stores each note verbatim
        with args.notes.open(encoding="utf-8") as fh:
            texts = {r["doc_id"]: r["text"] for r in map(json.loads, fh) if r}
    for p in args.notes.glob("*.txt") if args.notes.is_dir() else ():
        with p.open(encoding="utf-8", newline="") as fh:  # offsets index the file verbatim
            texts[p.name] = fh.read()
    by_doc: dict[str, list[dict[str, str]]] = {}
    for m in kept:
        m["mention_text"] = texts[m["doc_id"]][int(m["start"]) : int(m["end"])]
        by_doc.setdefault(m["doc_id"], []).append(m)
    for doc, ms in sorted(by_doc.items()):
        ms.sort(key=lambda m: (int(m["start"]), -int(m["end"])))
        for a, b in pairwise(ms):
            if int(b["start"]) < int(a["end"]):
                errors.append(f"{doc}: {a['mention_text']!r} overlaps {b['mention_text']!r}")

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    cols = ["doc_id", "mention_text", "occurrence", "start", "end", "group", "cui", *ATTRS]
    with (args.out / "mentions.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, [*cols, "annotator_note"], extrasaction="ignore")
        writer.writeheader()
        for doc in sorted(by_doc):
            writer.writerows(by_doc[doc])
    with (args.out / "spans_adjudicated.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, list(spans[0]))
        writer.writeheader()
        writer.writerows(spans)
    print(
        f"{len(kept)} mentions kept, {len(spans) - len(kept)} spans dropped, "
        f"{regrouped} groups set from semantic types",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
