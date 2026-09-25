# synthetic/

100 synthetic clinical notes, `doc_001.txt`–`doc_100.txt`: plain UTF-8 text, LF line endings,
about 30,000 words in all, across 18 note types (outpatient, emergency, inpatient progress,
consultation, operative, discharge and others). Patients are labelled `Synthetic-NNN`; there is
no real patient data. These are the notes cuiflow's examples and integration tests run on.

| files | what they are |
|---|---|
| `doc_021.txt`–`doc_100.txt` | 80 notes. No annotation, review or score has touched them: they are the held-out sample for the human-annotated reference. |
| `doc_001.txt`–`doc_020.txt` | the gold set's doc_01–20, lightly edited: list bullets `-` became `*` (offsets unchanged), doc_018 says "nasal drainage" for "nasal discharge", and doc_020 has no trailing rule. The gold labels for them, with offsets moved, are kept with the gold set (`tools/carry_gold.py` makes them). |

**Published scores come from the gold set.** The gold file stores its notes verbatim, so
`gold.jsonl` (kept outside this repository with the rest of the gold set) is the source of
record for the 30 gold notes; the carried-over labels are for comparing a human annotation of
doc_001–020 with Claude's. `python tools/export_notes.py <gold.jsonl> --out <folder>` writes
the gold notes out exactly.

Do not run `cuiflow evaluate` or the assertion review on `doc_021`–`doc_100` before the
human-sample plan is committed (its §5).
