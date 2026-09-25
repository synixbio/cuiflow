"""Blind review of engine assertions (DESIGN_PLAN §11 Phase 6; docs/GATES.md, protocol step 4).

Measures how often each mode's assertion labels are right without a full gold set: a reviewer
judges a random sample of each mode's positives and negatives for an attribute, without seeing
which label the engine gave.

    # 1. sample: runs the modes, writes a worksheet (note text: keep it where the notes live)
    #    and a key (engine labels); the reviewer sees only the worksheet
    python tools/assertion_review.py sample --notes <folder of .txt | reference .jsonl> `
        --modes single:mmlite,single:umlsmatch --per-cell 100 --out-dir review/
    # 2. the reviewer fills the worksheet's `judgement` column: yes / no / unclear
    # 3. score: aggregate figures only, safe to take out of the environment
    python tools/assertion_review.py score --dir review/ --out review/assertion_review.md

A mention found by several modes with the same span is one worksheet item, judged once. Per mode
and attribute the report gives the precision of positives (judged yes / judged), the negative
predictive value of negatives (judged no / judged), each with a Wilson 95% interval, and a
recall estimate from those two and the mode's positive and negative counts (the samples are
random within each stratum). Engine configuration comes from CUIFLOW_* and cuiflow.toml.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

FIVE = {"DISORDER", "FINDING", "DRUG", "PROCEDURE", "ANATOMY"}
QUESTIONS = {
    "negated": "Does the text say this is absent (not present, ruled out, resolved)?",
    "uncertain": "Does the writer hedge about it (possible, likely, rule out, versus)?",
    "history_of": "Is it presented as past history rather than part of this encounter?",
    "conditional": "Could it happen only if something else does?",
    "subject": "Is it about someone other than the patient?",
}


def _notes(path: Path) -> list[tuple[str, str]]:
    if path.is_dir():
        return [(p.name, p.read_text(encoding="utf-8")) for p in sorted(path.glob("*.txt"))]
    with path.open(encoding="utf-8") as fh:
        recs = [json.loads(line) for line in fh if line.strip()]
    return [(str(r.get("doc_id", r.get("id"))), r["text"]) for r in recs]


def _context(text: str, start: int, end: int, width: int = 160) -> str:
    lo = text.rfind("\n", 0, start) + 1
    hi = text.find("\n", end)
    hi = len(text) if hi < 0 else hi
    lo, hi = max(lo, start - width), min(hi, end + width)
    snippet = f"{text[lo:start]}[[{text[start:end]}]]{text[end:hi]}"
    return re.sub(r"\s+", " ", snippet).strip()


def _positive(attr: str, value: object) -> bool:
    return value not in (None, "patient") if attr == "subject" else value is True


def sample(args: argparse.Namespace) -> int:
    from cuiflow import Extractor, load_config

    notes = _notes(args.notes)
    attrs = args.attributes.split(",")
    # (mode, attr, positive?) -> list of (doc, start, end, cui)
    strata: dict[tuple[str, str, bool], list[tuple[str, int, int, str]]] = defaultdict(list)
    for mode in args.modes.split(","):
        with Extractor(load_config({"mode": mode})) as ex:
            for doc_id, text in notes:
                for m in ex.process(text, doc_id).mentions:
                    if m.semantic_group.upper() not in FIVE:
                        continue
                    for a in attrs:
                        v = getattr(m.assertions, a)
                        if v is not None:  # not assessed is not a negative
                            strata[(mode, a, _positive(a, v))].append(
                                (doc_id, m.start, m.end, m.cui)
                            )
    rng = random.Random(args.seed)
    texts = dict(notes)
    items: dict[tuple[str, int, int, str], str] = {}  # (doc, start, end, attr) -> item id
    key_rows, totals = [], {}
    for (mode, a, pos), found in sorted(strata.items()):
        unique = sorted({(d, s, e): c for d, s, e, c in found}.items())
        totals[f"{mode}|{a}|{'pos' if pos else 'neg'}"] = len(unique)
        for (d, s, e), c in rng.sample(unique, min(args.per_cell, len(unique))):
            items.setdefault((d, s, e, a), "")
            key_rows.append(
                {
                    "doc_id": d,
                    "start": s,
                    "end": e,
                    "attribute": a,
                    "cui": c,
                    "mode": mode,
                    "engine_positive": "yes" if pos else "no",
                }
            )
    order = list(items)
    rng.shuffle(order)
    for n, k in enumerate(order, 1):
        items[k] = f"R{n:04d}"
    for r in key_rows:
        r["item"] = items[(r["doc_id"], r["start"], r["end"], r["attribute"])]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "worksheet.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["item", "attribute", "question", "mention", "context", "judgement", "comment"])
        for k in sorted(order, key=lambda k: items[k]):
            d, s, e, a = k
            w.writerow([items[k], a, QUESTIONS[a], texts[d][s:e], _context(texts[d], s, e), "", ""])
    with (args.out_dir / "key.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(
            fh, ["item", "mode", "attribute", "engine_positive", "doc_id", "start", "end", "cui"]
        )
        w.writeheader()
        w.writerows(sorted(key_rows, key=lambda r: (r["item"], r["mode"])))
    (args.out_dir / "strata.json").write_text(
        json.dumps(
            {"notes": len(notes), "per_cell": args.per_cell, "seed": args.seed, "totals": totals},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"{len(order)} items to judge from {len(key_rows)} sampled labels in {len(notes)} notes")
    return 0


def _wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    z, p = 1.96, k / n
    mid = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (mid - half, mid + half)


def score(args: argparse.Namespace) -> int:
    with (args.dir / "worksheet.csv").open(encoding="utf-8-sig", newline="") as fh:
        judged = {r["item"]: r["judgement"].strip().lower() for r in csv.DictReader(fh)}
    bad = sorted(i for i, j in judged.items() if j not in ("yes", "no", "unclear"))
    if bad:
        print(
            f"{len(bad)} items without a yes/no/unclear judgement, e.g. {bad[:5]}", file=sys.stderr
        )
        return 1
    strata = json.loads((args.dir / "strata.json").read_text(encoding="utf-8"))
    cells: dict[tuple[str, str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    with (args.dir / "key.csv").open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            cells[(r["mode"], r["attribute"], "pos" if r["engine_positive"] == "yes" else "neg")][
                judged[r["item"]]
            ] += 1

    def rate(mode: str, a: str, side: str) -> tuple[str, float, int]:
        c = cells.get((mode, a, side), {})
        right = c.get("yes" if side == "pos" else "no", 0)
        n = c.get("yes", 0) + c.get("no", 0)
        if not n:
            return "n/a", math.nan, c.get("unclear", 0)
        lo, hi = _wilson(right, n)
        interval = f"({lo:.2f}–{hi:.2f})"  # noqa: RUF001  # an en dash, as in the report
        return f"{right}/{n} = {right / n:.2f} {interval}", right / n, c.get("unclear", 0)

    lines = [
        f"# Assertion review: {strata['notes']} notes, up to {strata['per_cell']} per cell, "
        f"seed {strata['seed']}",
        "",
        "Precision of positives (judged yes / judged) and negative predictive value of negatives "
        "(judged no / judged), Wilson 95% intervals; `unclear` is left out of both. Recall is "
        "estimated as PPV·P / (PPV·P + (1−NPV)·N) from the mode's P positives and "  # noqa: RUF001
        "N negatives.",
        "",
        "| mode | attribute | positives P | precision | negatives N | NPV | unclear "
        "| recall (est.) |",
        "|---|---|---:|---|---:|---|---:|---:|",
    ]
    for mode, a in sorted({(m, a) for m, a, _ in cells}):
        P = strata["totals"].get(f"{mode}|{a}|pos", 0)
        N = strata["totals"].get(f"{mode}|{a}|neg", 0)
        ptxt, ppv, pu = rate(mode, a, "pos")
        ntxt, npv, nu = rate(mode, a, "neg")
        tp, fn = ppv * P, (1 - npv) * N
        rec = f"{tp / (tp + fn):.2f}" if P and N and not math.isnan(tp + fn) and tp + fn else "n/a"
        lines.append(f"| `{mode}` | {a} | {P} | {ptxt} | {N} | {ntxt} | {pu + nu} | {rec} |")
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--notes", type=Path, required=True)
    s.add_argument("--modes", default="single:mmlite,single:umlsmatch")
    s.add_argument("--attributes", default="negated,uncertain,history_of")
    s.add_argument("--per-cell", type=int, default=100)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--out-dir", type=Path, required=True)
    c = sub.add_parser("score")
    c.add_argument("--dir", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    return sample(args) if args.cmd == "sample" else score(args)


if __name__ == "__main__":
    raise SystemExit(main())
