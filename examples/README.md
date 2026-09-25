# Examples

Scripts built on cuiflow's Python API: starting points to adapt, not part of the package.
Each has unit tests in [tests/test_examples.py](../tests/test_examples.py) that run on the fake
engine and a synthetic terminology store, so no UMLS data is needed to check them.

Run them with the Python that has cuiflow installed (activate `.venv`, or call
`.venv\Scripts\python`); the system `python` fails with `No module named 'cuiflow'`. Point cuiflow
at the engines the usual way: `cuiflow.toml`, `CUIFLOW_*` variables, or the flags each script
takes (see the main [README](../README.md#configuration)). For example:

```powershell
.venv\Scripts\Activate.ps1
$env:CUIFLOW_UMLSMATCH_DB="data/umls_sno_rx.sqlite"
$env:CUIFLOW_TERMINOLOGY_DB="data/terminology.sqlite"   # for --code
```

## `notes_of_interest.py`: which notes affirm a concept

Simple phenotyping. A note counts only when a target concept is **affirmed**: not negated,
about the patient, and not hedged (uncertain, conditional, hypothetical or generic). A past
history is affirmed; it is still the patient's. This is why the pipeline beats `grep`: on the 100
notes in `synthetic/`, `grep` finds "chest pain" in ten, and nine of those only deny it. The
script reports the same: one note affirms it, nine only deny it.

```powershell
python examples/notes_of_interest.py synthetic --term "chest pain" --show-excluded
python examples/notes_of_interest.py synthetic --cui C0011849 C0011860
python examples/notes_of_interest.py synthetic --code ICD10CM:I21 --terminology-db data\terminology.sqlite
python examples/notes_of_interest.py out\runs\<run-id> --term "chest pain" --save cohort.sqlite
```

- **`--term`** resolves the phrase with the same engine that scans the notes, keeps only the
  longest spans, and prints the result before scanning. "chest pain" becomes *Chest Pain*; the
  nested *Chest* and *Pain* are listed as not searched, since searching them would widen the
  cohort to every pain.
- **`--code SYSTEM:CODE`** takes the code **and every code below it** from the terminology store:
  `ICD10CM:I21` covers `I21.0` through `I21.B`, which UMLS 2026AA files under 64 CUIs. Only
  ICD-10-CM and SNOMED CT have a hierarchy, and the store needs `--mrrel` for it; otherwise the
  code matches only itself, and the script says so.
- **Sources** can be notes, run directories from `cuiflow extract --out-dir`, or
  `results.jsonl[.gz]` files. A run is read as it is, with no engine loaded unless `--term`
  needs one, so on a large corpus you extract once and query as often as you like (20,000
  notes in about 20 s). `--term` resolves with **the run's recorded config**, since another mode
  would resolve the phrase to other CUIs. The run's mentions keep that run's `--groups` and
  `--mention-filter`.
- **Every document is accounted for.** A note that fails to extract, or that a run recorded as
  failed, is listed as an error by key and exception type, never the message, which can quote
  the note. It is not quietly dropped from the denominator. The exit status is 1 when any
  document failed, and 2 for a usage or configuration error.
- **`--save PATH`** appends the search to SQLite (`search`, `concepts`, `documents`, `console`).
  `documents` lists every document with its status (`affirmed`, `excluded`, `absent`, `error`)
  and why it was excluded, because a cohort denominator can't be rebuilt from a list of hits.

**Unassessed is not absent.** An assertion a mode doesn't assess is `None`, and `None` never
excludes a mention. mmlite never assesses `subject`, and with NegEx it assesses nothing but
negation, so under `single:mmlite` a family-history or "possible" mention counts as affirmed.
The report says which of negation, subject and hedging went unassessed. `single:umlsmatch` (the
default) assesses all three: on one 20,000-note run, 3,600 notes mentioned diabetes without
affirming it, mostly as family history.

Everything this script prints or saves names notes by their document key, usually the file path.
Treat it as PHI.

## `note_summary.py`: what each note asserts

A chart abstract per note: problems, findings, medications and procedures, each split into
affirmed, negated, someone else's and hedged. It has one line per concept (CUI), not per mention,
so "HTN" and "hypertension" in one note are a single entry.

```powershell
python examples/note_summary.py synthetic\doc_001.txt --groups DISORDER DRUG --mention-filter guidelines --ingredients
python examples/note_summary.py out\runs\<run-id> --limit 5
python examples/note_summary.py out\runs\<run-id> --json > summaries.jsonl
```

```
== synthetic\doc_001.txt  (20 mentions, 19 concepts)
   Problems (DISORDER)
     affirmed:        Essential hypertension  [ICD10CM I10, SNOMEDCT_US 59621000]
                      Hyperlipidemia x2  [ICD10CM E78.5, SNOMEDCT_US 55822004]
                      Myocardial infarction  [ICD10CM I21, SNOMEDCT_US 22298006]
     negated:         Pneumothorax  [ICD10CM J93.9, SNOMEDCT_US 36118008]
   Medications (DRUG)
     affirmed:        amlodipine 10 MG  [RXNORM 329526; ingredient RXNORM 17767]
```

- A concept is **affirmed** when any mention of it is, by the same rule as
  `notes_of_interest.py`. Otherwise it goes under its most frequent other status. When its
  mentions disagree, the line says so, `(also negated 1)`. Those lines are the ones to check
  in the note.
- Concepts are **named by their preferred code's display** (SNOMED CT, then RxNorm, ICD-10-CM,
  LOINC). umlsmatch's preferred name can be any synonym: "EHT" for essential hypertension,
  "Eparina" for heparin.
- **Codes** are the ones on the mentions: `--codes` or `code_systems` in cuiflow.toml for notes,
  and whatever a run was extracted with. `--ingredients` adds each RxNorm product's ingredients
  when they differ from the product.
- `history_of` is not shown. It is often wrong, and a past history is still the patient's.

## `concept_matrix.py`: a document x concept feature table

One row per document and one column per CUI, for cohort analytics or as model features. A cell
is `1` when the note affirms the concept, `-1` when it only mentions it negated, about someone
else, or hedged, and `0` when it doesn't mention it. A bag-of-CUIs matrix would treat "denies
chest pain" as chest pain.

```powershell
python examples/concept_matrix.py out\runs\<run-id> -o features.csv --groups DISORDER DRUG --min-docs 3
```

- **Every processed note is a row**, including notes with no concept (all zeros), so rates
  computed from the matrix have the right denominator. A note that failed is **left out** and
  listed on stderr, and the exit status is 1. It is never written as an all-zero row, where it
  would pass for a note that mentions nothing.
- `features.concepts.csv` describes each column: CUI, name, group, and how many documents affirm
  it and mention it without affirming it. `--min-docs N` keeps concepts affirmed in at least N
  documents.
- **Read the report before using the matrix.** It ranks concepts by how many documents affirm
  them, which exposes dictionary collisions fast. On the 100 notes in `synthetic/` under
  `single:umlsmatch`, the most affirmed disorder is *Infantile neuroaxonal dystrophy* (64
  notes), matched from the heading "PLAN", and the third is *Body integrity dysphoria* (28
  notes), matched from "BID". `--mention-filter guidelines` removes the first but not the second,
  so drop such columns before modelling.

## `highlight_note.py`: review mentions in the note text

Writes a self-contained HTML page with each note's mentions colored by status (affirmed,
negated, someone else's, hedged). Hover over a highlight to see its concepts and the assertions
that were assessed. Overlapping mentions are underlined. The legend hides a status, and a table
under each note lists every mention with its offsets and codes.

```powershell
python examples/highlight_note.py synthetic\doc_001.txt -o review.html
python examples/highlight_note.py synthetic -o review.html --limit 5 --mode single:mmlite
```

Use it to check a mode before a study, or to explain a surprising count. It reads notes only,
since a run keeps the text only when extracted with `include_text`, and it stops at 20 notes
unless `--limit` says otherwise. **The page contains the notes**, so it is PHI. It loads
nothing from the network.

## Where the metamap_api examples went

These scripts started as copies of mmlite's `examples/`. Most of them are now built into cuiflow,
so they were removed:

| mmlite example | In cuiflow |
|---|---|
| `analyze_file.py`, `analyze_folder.py` | `cuiflow extract` (`--out-dir`, `--workers`, `--resume`) |
| `parse_to_{csv,jsonl,jsonl_batch,parquet,sqlite}.py`, `_runs.py`, `_annotations.py` | `cuiflow extract -o` / `cuiflow export` (JSONL, Parquet, SQLite, OMOP `NOTE_NLP`), with run manifests |
| `json_to_sqlite.py`, `load_to_sqlite.py` | `cuiflow export <run> -o mentions.sqlite` |
| `compare_exports.py` | every format is written from one `results.jsonl`, so there is nothing to reconcile |
| `filter_mrconso.py`, `filter_mrrel.py` | `cuiflow build-terminology` reads MRCONSO and MRREL once each |
| `add_{snomed,icd10cm,rxnorm,loinc}_codes.py`, `create_combined_codes_view.py` | `--codes` (the preferred-code rules are in DESIGN_PLAN §5.2) |
| `add_icd10cm_hierarchy.py`, `add_snomedct_hierarchy.py` | `--ancestors N`; `TerminologyStore.ancestors()` / `.descendants()` |
| `add_rxnorm_ingredients.py` | `--ingredients`; `TerminologyStore.ingredients()` |

The UMLS rules those scripts verified by hand (MRREL column direction, code-level hierarchy
walks, the RxNorm Brand Name false-ingredient exclusion) were ported with their tests to
[tests/test_hierarchy.py](../tests/test_hierarchy.py) and checked on real data in
[docs/GATES.md](../docs/GATES.md).
