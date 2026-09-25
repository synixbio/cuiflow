"""Agreement between two annotators' reference files (guidelines §7.2).

Span + CUI F1, span F1 and overlap F1 over mentions, and Cohen's kappa for each attribute on the
mentions both annotators marked with the same span. Writes a Markdown report and, with
``--disagreements``, a CSV of every mention the two do not agree on.

    python tools/agreement.py A.jsonl B.jsonl --out agreement.md --disagreements diff.csv
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

ATTRS = ("negated", "subject", "history_of", "uncertain", "conditional", "generic")


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


def _f1(a: int, b: int, both: int) -> float:
    return 2 * both / (a + b) if a + b else 1.0


def _overlap_matches(a: dict, b: dict) -> int:
    """Greedy one-to-one matching of overlapping spans in the same document."""
    used: set = set()
    n = 0
    for doc, s, e in sorted(a):
        for key in sorted(b):
            if key in used or key[0] != doc:
                continue
            if key[1] < e and s < key[2]:
                used.add(key)
                n += 1
                break
    return n


def _kappa(pairs: list[tuple[object, object]]) -> float | None:
    if not pairs:
        return None
    n = len(pairs)
    po = sum(x == y for x, y in pairs) / n
    ca, cb = Counter(x for x, _ in pairs), Counter(y for _, y in pairs)
    pe = sum(ca[k] * cb[k] for k in ca.keys() | cb.keys()) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def _positive(attr: str, v: object) -> bool:
    return v != "patient" if attr == "subject" else v is True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("a", type=Path)
    ap.add_argument("b", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--disagreements", type=Path)
    args = ap.parse_args()

    texts, a = _load(args.a)
    _, b = _load(args.b)
    shared = a.keys() & b.keys()
    same_cui = [k for k in shared if a[k]["cui"] == b[k]["cui"]]
    name_a, name_b = args.a.parent.name, args.b.parent.name

    lines = [
        f"# Annotator agreement: {name_a} / {name_b}",
        "",
        f"{len({k[0] for k in a | b})} notes. `{args.a.as_posix()}` against `{args.b.as_posix()}`. "
        "Spans match exactly unless stated; CUI-LESS counts as a value.",
        "",
        "| | mentions | " + " | ".join(f"{t} +" for t in ATTRS) + " |",
        "|---|---:|" + "---:|" * len(ATTRS),
    ]
    for name, ms in ((name_a, a), (name_b, b)):
        pos = [sum(_positive(t, m["assertions"].get(t)) for m in ms.values()) for t in ATTRS]
        lines.append(f"| {name} | {len(ms)} | " + " | ".join(map(str, pos)) + " |")
    lines += [
        "",
        "| span + CUI F1 | span F1 | overlap F1 | shared spans | same span, different CUI |",
        "|---:|---:|---:|---:|---:|",
        f"| {_f1(len(a), len(b), len(same_cui)):.3f} | {_f1(len(a), len(b), len(shared)):.3f} "
        f"| {_f1(len(a), len(b), _overlap_matches(a, b)):.3f} | {len(shared)} "
        f"| {len(shared) - len(same_cui)} |",
        "",
        f"Attributes on the {len(shared)} shared spans (generic only where both judged it):",
        "",
        "| attribute | agree | Cohen's kappa | positives " + f"{name_a} / {name_b} |",
        "|---|---:|---:|---|",
    ]
    for t in ATTRS:
        pairs = [(a[k]["assertions"].get(t), b[k]["assertions"].get(t)) for k in sorted(shared)]
        pairs = [p for p in pairs if p[0] is not None and p[1] is not None]
        k = _kappa(pairs)
        agree = sum(x == y for x, y in pairs)
        pa = sum(_positive(t, x) for x, _ in pairs)
        pb = sum(_positive(t, y) for _, y in pairs)
        lines.append(
            f"| {t} | {agree}/{len(pairs)} | {'n/a' if k is None else f'{k:.3f}'} | {pa} / {pb} |"
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.disagreements:
        rows = []
        for key in sorted(a.keys() | b.keys()):
            doc, s, e = key
            ma, mb = a.get(key), b.get(key)
            if ma and mb:
                diff = [t for t in ATTRS if ma["assertions"].get(t) != mb["assertions"].get(t)]
                if ma["cui"] != mb["cui"]:
                    diff.insert(0, "cui")
                if not diff:
                    continue
                kind = "labels"
            else:
                diff, kind = [], f"only {name_a if ma else name_b}"
            rows.append(
                {
                    "doc_id": doc,
                    "start": s,
                    "end": e,
                    "text": texts[doc][s:e],
                    "kind": kind,
                    "differs": " ".join(diff),
                    f"cui_{name_a}": ma["cui"] if ma else "",
                    f"cui_{name_b}": mb["cui"] if mb else "",
                    f"labels_{name_a}": json.dumps(ma["assertions"]) if ma else "",
                    f"labels_{name_b}": json.dumps(mb["assertions"]) if mb else "",
                }
            )
        with args.disagreements.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, list(rows[0]) if rows else ["doc_id"])
            w.writeheader()
            w.writerows(rows)
    print(args.out.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
