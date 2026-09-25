# Phase gates: evidence

What was run to check the DESIGN_PLAN §11 gates for Phases 2, 4, 5 and 6, what it showed, and
where the evidence falls short. Phases 1 and 3 are in [EVALUATION.md](EVALUATION.md). Every figure names the data and notes that produced it, and the
commands reproduce it.

**Environment (2026-09-23):** Windows 11, Intel i7-13700H (20 logical CPUs), 40 GB RAM,
Python 3.14.6. UMLS 2026AA (`<META>`), the umlsmatch
dictionary `umls_sno_rx.sqlite` and mmlite index `ivf`, both built from 2026AA. OMOP vocabulary:
Athena download with SNOMED 2024-08-01, RxNorm 20241202, ICD10CM FY2025 and LOINC 2.78.
PostgreSQL 15.3.

All notes are synthetic: `tools/synth_corpus.py` (seed 0) for scale, the 30 gold notes
(doc_01–30) for the gold set, and the 100 notes in `synthetic/` for examples and parity checks.
No real note was used. The gold notes are stored verbatim in the gold file (`gold.jsonl`), which is
kept outside this repository with the rest of the gold set;
`python tools/export_notes.py <gold.jsonl> --out notes` writes them back out as `.txt` files.

## Phase 2: terminology store (passed)

```powershell
cuiflow build-terminology --mrconso $META/MRCONSO.RRF --mrrank $META/MRRANK.RRF `
    --mrrel $META/MRREL.RRF --out data/terminology.sqlite
$env:CUIFLOW_TERMINOLOGY_DB="data/terminology.sqlite"
pytest -m integration tests/integration/test_terminology_gate.py
```

The build takes 2 min 56 s and produces 1.7 GB: 2,008,753 code rows, 585,746 ICD-10-CM and
7,774,767 SNOMED CT ancestor rows (every code, walked to the roots), and 135,682 RxNorm ingredient
rows.

| Case from metamap_api's examples | Result on 2026AA |
|---|---|
| Orthopnea's chain through `R06` | `R06.01` → `R06.0` (1) → `R06` (2) → `R00-R09` (3) → `R00-R99` (4) → `ICD-10-CM` (5). `R06` shares its CUI with `R06.9` and is kept |
| "Jaw" → 661005 | C0022359 has no SNOMED CT PT row; the MRRANK fallback makes 661005 (SY "Jaw") preferred |
| Lisinopril's ingredient paths | SCD 314076 → lisinopril (RxCUI 29046) in 2 hops. Every Zestril and Prinivil SBD → 29046, never the brand-name node. HCTZ/lisinopril 197885 → both 5487 and 29046 |
| One CUI, two SNOMED CT chains | lisinopril substance 386873009 and product 108575001 have disjoint parents |

The synthetic regression cases ported with the code (the DAG case, the code-level walk, the
dead-end brand name, `has_tradename` ignored, suppressed edges) are in `tests/test_hierarchy.py`.

## Phase 4: CLI and batch (passed, with one caveat)

```powershell
python tools/synth_corpus.py --notes 2000  --out out/synth/small.jsonl
python tools/synth_corpus.py --notes 20000 --out out/synth/large.jsonl
$F = "--mode single:umlsmatch --umlsmatch-db <db> --terminology-db data/terminology.sqlite --codes SNOMEDCT_US,RXNORM,ICD10CM"
cuiflow extract out/synth/small.jsonl --out-dir out/runs --run-id scale-small-rss $F
cuiflow extract out/synth/large.jsonl --out-dir out/runs --run-id scale-large $F   # killed, then:
cuiflow extract out/synth/large.jsonl --resume out/runs/scale-large $F
```

**A corpus larger than memory completes: shown by flat memory, not by a literal
larger-than-RAM corpus.** Input is streamed and at most `workers × 4` documents are in flight.
Peak resident memory of the whole in-process run (engine, store and writer) does not grow with
corpus size:

| Run (1 worker, `single:umlsmatch`, codes attached) | Documents | Mentions | docs/s | Peak RSS |
|---|---|---|---|---|
| `scale-small-rss` | 2,000 | 137,024 | 41.1 | 171.7 MB |
| `scale-large`, resumed session (skipped 9,801 already done) | 10,199 | 696,970 | 47.5 | 173.6 MB |

A resume indexes the (key, content hash) pairs already done in a temporary SQLite file in the
run directory, removed when the session ends, rather than holding them in memory (about 150
bytes per document, 1.5 GB at ten million). Resuming the finished 20,000-document run
`scale-resume-all` skipped all 20,000 in 1.5 s at 102.8 MB peak, less than a processing run,
because no document was processed.

**A killed run resumes without duplicating documents.** `scale-large` was hard-killed
(`Stop-Process -Force`, TerminateProcess: no cleanup runs) after 9,800 of 20,000 documents and
then resumed:

- The results file holds exactly 20,000 lines: every note id from 1 to 20,000, each once. The
  resume skipped 9,801 documents (one more had been written between counting and the kill),
  processed the remaining 10,199, and had 0 errors.
- The killed session appears in `previous_sessions` with `finished_at: null`. It was recorded with
  `documents: 0`, because the manifest was then only rewritten when a run finished. The batch
  runner now checkpoints the manifest every 1,000 documents, so a killed session keeps its counts,
  and the kill test asserts this.
- `tests/test_resume.py` repeats this with a subprocess kill and a deliberately torn last line.
  Before this phase, resuming after a torn line would have glued the next document onto it and
  corrupted both. The torn tail is now cut first.

**The manifest is complete.** Beyond versions, effective configuration, input checksums,
engine and terminology `build_info` (with `umls_release`), throughput, peak RSS and errors, a
resumed run records:

- every earlier session in `previous_sessions`, with a killed one marked `finished_at: null`;
- `documents_in_results`;
- every `cuiflow export` made from it.

A resume under different settings is refused, and so are:

- a resume that names its text inputs differently (`notes/` vs an absolute path, or another
  working directory), because a text document's key is its path as typed and every document
  would be done again;
- a resume whose engines or terminology report another identity (version, UMLS release, build)
  than the session that recorded them;
- a second session on a run directory another session holds. The lock is an OS file lock, so a
  killed session's lock never outlives it.

The Windows peak-RSS helper had been silently
returning `null` because of undeclared ctypes signatures. That is fixed and tested.

**`NOTE_NLP` loads into a v5.4 schema.**

```powershell
cuiflow build-omop-vocab --athena <Athena download> --out data/omop_vocab.sqlite   # 46 s
cuiflow export out/runs/scale-small -o out/omop/note_nlp.csv --note-id-from-doc-id `
    --omop-vocab data/omop_vocab.sqlite
```

I loaded the file into a scratch PostgreSQL database with the official OHDSI v5.4 DDL for
`omop.note_nlp` (`ddl_5.4.sql`) and its primary key (`primary_key_5.4.sql`), using
`\copy ... WITH (FORMAT csv, HEADER true)`, then dropped the database.

- **All 137,024 rows loaded**: 2,000 notes, 133,902 rows (97.7%) with a standard concept.
- `term_modifiers` is at most 97 characters, and nothing hit a length limit.

This check found a design error. DESIGN_PLAN §8.2 specified `note_nlp_id` as a 63-bit hash, but
v5.4 declares the column `integer`, which is 32-bit in PostgreSQL. With the hash ids the load
fails with `value "1662371003810395518" is out of range for type integer`. The default is now
sequential ids, which are deterministic for a given results file and can start at any
`--first-id`. The hash is kept as `--id-scheme hash63` for sites that widen the column to
`bigint`. `tests/test_writers.py` enforces the 32-bit range on a v5.4-shaped table.

The same run exports to Parquet (3.3 MB, 3 row groups, 6.7 s) and SQLite (84 MB, 572k code rows,
4.8 s).

## Phase 5: REST and MCP (gate passed)

**A local MCP client calls every tool successfully.** `tests/test_services.py` spawns an MCP
server over real stdio and drives it with the SDK's `Client`: all four tools and both resources.
It runs in CI with a fake engine and a synthetic store.

**Warm latency within 200 ms in `single:*` modes; startup reported separately.** The client-side
round trip over stdio, with a different synthetic note on each call (median 738 characters) and
SNOMED CT, RxNorm and ICD-10-CM codes attached:

```powershell
python tools/bench_mcp.py --mode single:umlsmatch --umlsmatch-db <db> --mmlite-index <ivf> `
    --terminology-db data/terminology.sqlite --calls 100
```

| Mode | Startup (spawn → initialized) | First call | Warm p50 | Warm p95 | Warm max |
|---|---|---|---|---|---|
| `single:umlsmatch` | 2.0 s | 441 ms | 23.8 ms | 41.0 ms | 89.8 ms |
| `single:mmlite` | 2.3 s | 296 ms | 16.4 ms | 36.1 ms | 72.9 ms |

The first call pays for lazy loading, so it is reported apart from the warm calls. Both modes
pass with a wide margin.

**Metrics.** `/metrics` (Prometheus) is behind the bearer token. A test checks that no label or
value carries the note, the document key or a CUI.

**Streamable HTTP.** `cuiflow mcp --transport http` serves the same tools at `/mcp` behind
`CUIFLOW_API_TOKEN`. `tests/test_services.py` drives it from an MCP client over a real socket,
and checks that a missing or wrong token gets 401. Latency above was measured over stdio only.

## Phase 6: scale and hardening (not passed)

The gate asks that every published figure be reproducible from the repository, stated with the
dictionary and note set that produced it, and that the tool be validated on real notes. The
first part is met. The second has been dry-run on synthetic notes
([below](#real-note-validation-dry-run-on-synthetic-notes-2026-09-23)) but not run on real notes.

### Throughput and memory at 1, 4 and 8 workers

Codes attached (SNOMED CT, RxNorm, ICD-10-CM). "Workers" is each worker process's own peak RSS;
the parent process (reading, writing, the terminology store) is listed separately.

```powershell
cuiflow extract out/synth/large.jsonl --out-dir out/runs --run-id scale-w4 --workers 4 --mode single:umlsmatch $F
cuiflow extract out/synth/small.jsonl --out-dir out/runs --run-id scale3-single-mmlite-w4 --workers 4 --mode single:mmlite $F
```

| mode | documents | workers | elapsed | docs/s | peak RSS per worker | parent |
|---|---:|---:|---:|---:|---:|---:|
| `single:umlsmatch` | 20,000 | 1 | 445 s | 44.9 | 174 MB (in-process) | n/a |
| `single:umlsmatch` | 20,000 | 4 | 139 s | 143.4 | 170 MB | 41 MB |
| `single:umlsmatch` | 20,000 | 8 | 104 s | 191.5 | 170 MB | 46 MB |
| `single:mmlite` | 2,000 | 1 | 59 s | 34.2 | 156 MB (in-process) | n/a |
| `single:mmlite` | 2,000 | 4 | 44 s | 45.5 | 149 MB | 43 MB |
| `single:mmlite` | 2,000 | 8 | 46 s | 43.9 | 148 MB | 48 MB |
| `ensemble:consensus` | 2,000 | 1 | 198 s | 10.1 | 217 MB (in-process) | n/a |
| `ensemble:consensus` | 2,000 | 4 | 56 s | 35.9 | 210 MB | 39 MB |
| `ensemble:consensus` | 2,000 | 8 | 58 s | 34.2 | 209 MB | 42 MB |

**Results do not depend on the worker count.** For each mode, the results files at 1, 4 and 8
workers are identical once the per-document `elapsed_ms` is removed.

**Memory is the solid result.** Each worker's peak is flat with the worker count: about 150 MB for
mmlite, 170 MB for umlsmatch and 210 MB for a two-engine mode. Budget workers × that, plus about
50 MB for the parent. This is below DESIGN_PLAN's starting estimate for two long-lived engines
(0.6–0.8 GB), measured over runs of minutes, not days.

**Throughput is weaker evidence.**
- The 20,000-note umlsmatch runs are the scaling result: 3.2× at 4 workers and 4.3× at 8.
- The 2,000-note runs are dominated by start-up. Each worker loads its engines first (3.5 s for
  umlsmatch, up to 9.5 s for mmlite, measured separately), so 4 and 8 workers barely differ.
- This laptop's timings vary between runs: a first batch of the 2,000-note runs, taken while
  other work was running, differed by up to 2.5× and was discarded (`scale2-*`; the figures
  above are the quiet `scale3-*` reruns).
- Measure on the deployment machine, with the real corpus size, before sizing a pool.

### Every published figure, and how to reproduce it

| figure | where | data | command |
|---|---|---|---|
| Baselines against Java cTAKES and MetaMapLite | EVALUATION.md | umlsmatch's 20 notes; mmlite's 12 parity documents | `cuiflow evaluate --format ctakes-silver` / `mmlite-fixtures` (EVALUATION.md) |
| Gold-set scores and the Phase 3 gate | EVALUATION.md | `gold.jsonl` (30 synthetic notes) | `cuiflow evaluate --format cuiflow --reference <gold.jsonl>` |
| Blind assertion review (Phase 6 dry run) | this file, Phase 6 | the 30 gold notes; engine output | `python tools/assertion_review.py sample` / `score` (below) |
| Agreement of the two annotations of doc_21–30 | the annotation records | `new_notes/` and `new_notes_second/gold.jsonl` | `python tools/agreement.py` |
| Annotator agreement | the annotation records | the three worksheets | produced before this repository's tools; not reproducible from it yet |
| Terminology build and gate cases | Phase 2 above | UMLS 2026AA | `cuiflow build-terminology`, `pytest -m integration` |
| Scale, resume and `NOTE_NLP` load | Phases 4 and 6 | `tools/synth_corpus.py`, seed 0 | the commands above |
| MCP latency | Phase 5 above | `tools/synth_corpus.py` notes | `python tools/bench_mcp.py` |
| Adapter parity (Phase 1) | DESIGN_PLAN §11 | the 30 notes in `gold.jsonl` (via `CUIFLOW_GOLD`) and the 100 in `synthetic/` | `pytest -m integration tests/integration/test_engines.py` |

Each figure needs the UMLS 2026AA files, the umlsmatch dictionary and the mmlite index, all of
which are licensed and stay outside the repository. The gold-set figures also need the gold set
and its annotation records, which are kept outside the repository too.

### Real-note validation: protocol (not run)

This needs notes this project does not have. It must run where the notes live, with egress
blocked, and only aggregate scores may leave. The human-sample plan, kept with the gold set,
covers the step before this (a human reference on the synthetic notes), sample sizes, the
analysis plan to commit first, and what counts as human-annotated.

1. **Notes:** 50–100 de-identified notes approved for this use, stratified by note type.
   Nothing about them enters this repository.
2. **Annotation:** two people annotate under the signed-off guidelines, with a 20% overlap and a
   clinician adjudicating (GUIDELINES §7). `cuiflow gold convert` and `gold validate` run
   locally.
3. **Scoring:** `cuiflow evaluate --format cuiflow` on every mode, without `--per-document`,
   since per-document rows carry note keys. Report the dictionary, the note types and counts,
   and the date.
4. **Adjudicated assertion checks:** a clinician judges a random sample of each engine's
   `negated`, `uncertain` and `history_of` positives and negatives (at least 100 of each), as
   umlsmatch's own adjudication did. This measures assertion precision without a full gold set.
5. **The gate:** the Phase 3 decisions are rerun on this set. A mode ships as supported only if
   it passes here.

### Real-note validation: dry run on synthetic notes (2026-09-23)

The protocol above was run end to end with synthetic stand-ins, to check that each step works
and produces only aggregate output. **This does not meet the gate**: no real note was used, and
every judgement is Claude's.

| step | real run | dry run |
|---|---|---|
| 1. Notes | 50–100 approved de-identified notes, stratified by type | the 30 synthetic gold notes, 27 note types |
| 2. Annotation | two people, 20% overlap, a clinician adjudicating | the gold set: doc_21–30 double-annotated and adjudicated, all by Claude |
| 3. Scoring | `cuiflow evaluate`, no `--per-document` | as specified, on the gold set ([EVALUATION.md](EVALUATION.md#gold-set)) |
| 4. Assertion checks | a clinician judges ≥ 100 positives and ≥ 100 negatives per engine and attribute | Claude judged a blind sample of 100 per cell, or every positive where there were fewer |
| 5. Gate | Phase 3 rerun on this set | consensus passes; union and hybrid_staged fail |

**Step 4 tool.** `tools/assertion_review.py` implements step 4:

- **`sample`** runs the modes over the notes. It samples each mode's positives and negatives
  per attribute within the five-group scope, and writes three files:
  - `worksheet.csv`: mention, context and a question, with no engine label;
  - `key.csv`: which engine gave which label;
  - `strata.json`: the counts.
  A span found by both engines is one item, judged once.
- **`score`** reads the filled worksheet and writes only aggregates: precision of positives,
  NPV of negatives, a recall estimate, and Wilson intervals.

The worksheet and key quote the notes, so on real notes they stay where the notes are. Only the
report leaves.

```powershell
python tools/assertion_review.py sample --notes <gold.jsonl> `
    --modes single:mmlite,single:umlsmatch --per-cell 100 --seed 0 --out-dir docs/eval/2026-09-23-assertion-review
# fill worksheet.csv's judgement column: yes / no / unclear
python tools/assertion_review.py score --dir docs/eval/2026-09-23-assertion-review `
    --out docs/eval/2026-09-23-assertion-review/assertion_review.md
```

**Results.** 703 items, 738 sampled labels. Judged 174 yes, 459 no, 70 unclear. `unclear` was
used for a fragment inside the phrase a cue governs, such as "extremity" in "Denies lower
extremity numbness", where the label says nothing about the engine's cue handling.

| mode | attribute | positives | precision (95% CI) | NPV (95% CI) | recall (est.) |
|---|---|---:|---|---|---:|
| `single:mmlite` | negated | 134 | 0.90 (0.83–0.95) | 0.98 (0.93–0.99) | 0.69 |
| `single:umlsmatch` | negated | 277 | 0.81 (0.71–0.88) | 0.99 (0.94–1.00) | 0.91 |
| `single:umlsmatch` | uncertain | 58 | 0.78 (0.62–0.88) | 1.00 (0.96–1.00) | 1.00 |
| `single:umlsmatch` | history_of | 80 | **0.34** (0.24–0.46) | 0.99 (0.94–1.00) | 0.52 |

What the errors are:

- **`history_of`, umlsmatch:** 26 of its 46 wrong positives are the words of the heading
  "HISTORY OF PRESENT ILLNESS" ("PRESENT", "ILLNESS"). Most of the rest are this admission's
  events ("Dyspnea and orthopnea fully resolved"), a duration read as history ("a 4-month history
  of worsening joint pain"), and a coworker's current illness. Leaving out the heading words,
  precision would be about 0.55. The guideline post-filter already drops headings.

  cuiflow's heading rule handles these: `Extractor` sets `history_of` to not assessed on every
  mention inside a heading, whatever the filter. On the gold notes this clears exactly the 26 heading positives
  (umlsmatch's positives go from 80 to 54; a test checks it against this review), so on the
  reviewed labels precision is 24/44 = 0.55. The other errors remain. On the gold set the
  rule changes no concept or assertion P/R/F1; `history_of` coverage drops by at most 0.002.
- **`negated`, umlsmatch:**
  - the word "negative" read as a negation cue: "tuberculin skin test ... was negative", "first
    negative blood culture";
  - headings and anatomy near a negated list ("ASSESSMENT AND PLAN", "mediastinum");
  - one procedure step, "The posterior longitudinal ligament was incised".
- **`negated`, mmlite:** qualifiers next to a negation ("calves soft", "bowel sounds present"),
  and "Neuromyelitis optica is unlikely", which is uncertain, not absent.
- **`uncertain`, umlsmatch:** things next to a hedge rather than hedged themselves: the drugs in
  "Suspected pneumonia: start ceftriaxone", the "opacity" before "possibly pneumonia", "recent
  travel" in "Concern for PE given recent travel".

These figures are on the engines' own spans, including headings and fragments, which the gold
set leaves out. So they are lower than the gold-set assertion scores, which count only mentions
matched to a gold span.

**Self-consistency.** On the 232 reviewed items that are also gold mentions, the review agrees
with the gold label every time. Claude wrote both, so this shows consistency, not validity.

**What the real run needs that the dry run could not supply:** real notes under PHI controls; a
clinician, not the annotation's author, for step 4; two independent annotators; and more than
30 notes, since several cells here have fewer positives than the protocol asks for (uncertain:
58, history_of: 80).
