#!/usr/bin/env python
"""Generate a synthetic clinical-note corpus for batch and scale tests (DESIGN_PLAN §11, Phase 4).

Every note is invented from templates: no patient is behind any of it, and nothing here is
copied from a real note. The same seed always produces the same corpus, so runs over it are
reproducible.

    python tools/synth_corpus.py --notes 100000 --out out/synth/notes.jsonl
    python tools/synth_corpus.py --notes 500 --out out/synth/notes/        # one .txt per note

JSONL records are ``{"id": "<integer>", "text": ...}``: integer ids so an OMOP export can use
``--note-id-from-doc-id``. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Iterator
from pathlib import Path

PROBLEMS = [
    "hypertension",
    "type 2 diabetes mellitus",
    "hyperlipidemia",
    "chronic kidney disease",
    "atrial fibrillation",
    "asthma",
    "chronic obstructive pulmonary disease",
    "hypothyroidism",
    "gastroesophageal reflux disease",
    "osteoarthritis of the knee",
    "major depressive disorder",
    "obstructive sleep apnea",
    "coronary artery disease",
    "congestive heart failure",
    "migraine",
]
SYMPTOMS = [
    "chest pain",
    "shortness of breath",
    "orthopnea",
    "palpitations",
    "fever",
    "chills",
    "cough",
    "nausea",
    "vomiting",
    "abdominal pain",
    "headache",
    "dizziness",
    "fatigue",
    "leg swelling",
    "back pain",
    "joint pain",
    "rash",
    "weight loss",
]
DRUGS = [
    ("lisinopril", "10 mg daily"),
    ("metformin", "500 mg twice daily"),
    ("atorvastatin", "40 mg nightly"),
    ("amlodipine", "5 mg daily"),
    ("levothyroxine", "75 mcg daily"),
    ("omeprazole", "20 mg daily"),
    ("albuterol", "2 puffs as needed"),
    ("apixaban", "5 mg twice daily"),
    ("furosemide", "20 mg daily"),
    ("sertraline", "50 mg daily"),
    ("aspirin", "81 mg daily"),
    ("hydrochlorothiazide", "12.5 mg daily"),
]
RELATIVES = ["mother", "father", "sister", "brother", "maternal grandmother"]
FAMILY_CONDITIONS = ["breast cancer", "colon cancer", "diabetes", "stroke", "heart disease"]
EXAMS = [
    "Lungs are clear to auscultation bilaterally.",
    "Heart has a regular rate and rhythm without murmurs.",
    "Abdomen is soft and nontender.",
    "No lower extremity edema.",
    "Mild tenderness over the lumbar spine.",
    "Trace bilateral ankle edema.",
]
PLANS = [
    "Continue current medications.",
    "Check a basic metabolic panel and lipid panel.",
    "Follow up in three months.",
    "Referred to cardiology for an echocardiogram.",
    "Start a low-sodium diet.",
    "Return if symptoms worsen or fever develops.",
    "Consider a sleep study if snoring persists.",
]


def _note(rng: random.Random) -> str:
    age = rng.randint(24, 89)
    sex = rng.choice(["man", "woman"])
    present = rng.sample(SYMPTOMS, rng.randint(1, 3))
    denied = rng.sample([s for s in SYMPTOMS if s not in present], rng.randint(1, 4))
    history = rng.sample(PROBLEMS, rng.randint(1, 4))
    meds = rng.sample(DRUGS, rng.randint(1, 4))
    relative = rng.choice(RELATIVES)
    parts = [
        "CHIEF COMPLAINT:",
        f"{present[0].capitalize()}.",
        "",
        "HISTORY OF PRESENT ILLNESS:",
        f"{age}-year-old {sex} presents with {', '.join(present)} for "
        f"{rng.randint(2, 21)} days. Denies {', '.join(denied)}.",
    ]
    if rng.random() < 0.3:
        parts.append(f"Possible {rng.choice(PROBLEMS)}, to be evaluated.")
    parts += [
        "",
        "PAST MEDICAL HISTORY:",
        *(f"- {h.capitalize()}" for h in history),
        "",
        "MEDICATIONS:",
        *(f"- {name.capitalize()} {dose}" for name, dose in meds),
        "",
        "FAMILY HISTORY:",
        f"{relative.capitalize()} with {rng.choice(FAMILY_CONDITIONS)}.",
        "",
        "PHYSICAL EXAM:",
        " ".join(rng.sample(EXAMS, 3)),
        "",
        "ASSESSMENT AND PLAN:",
        f"{history[0].capitalize()}, stable. " + " ".join(rng.sample(PLANS, 2)),
    ]
    # Padding, so notes vary in length like real ones.
    for _ in range(rng.randint(0, 6)):
        symptom = rng.choice(SYMPTOMS)  # drawn before the trend: the order fixes the corpus
        trend = rng.choice(["better", "unchanged", "worse"])
        parts.append(f"Patient reports {symptom} is {trend}.")
    return "\n".join(parts) + "\n"


def notes(n: int, seed: int = 0) -> Iterator[tuple[int, str]]:
    """``(note number, text)`` for ``n`` notes; the same seed gives the same corpus."""
    rng = random.Random(seed)
    for i in range(1, n + 1):
        yield i, _note(rng)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--notes", type=int, required=True, help="how many notes")
    ap.add_argument(
        "--out", type=Path, required=True, help="a .jsonl file, or a folder for .txt files"
    )
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    if args.out.exists() and (args.out.is_file() or any(args.out.iterdir())):
        print(f"error: {args.out} exists and is not empty", file=sys.stderr)
        return 1
    size = 0
    if args.out.suffix == ".jsonl":
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8", newline="\n") as fh:
            for i, text in notes(args.notes, args.seed):
                line = json.dumps({"id": str(i), "text": text}) + "\n"
                fh.write(line)
                size += len(line)
    else:
        args.out.mkdir(parents=True, exist_ok=True)
        for i, text in notes(args.notes, args.seed):
            (args.out / f"note_{i:07d}.txt").write_text(text, encoding="utf-8", newline="\n")
            size += len(text)
    print(f"{args.notes:,} notes, {size / 1e6:.1f} MB -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
