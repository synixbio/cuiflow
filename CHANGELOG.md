# Changelog

## 0.1.0 (2026-09-25)

cuiflow extracts clinical concepts from free-text notes with
[mmlite](https://pypi.org/project/mmlite/), [umlsmatch](https://pypi.org/project/umlsmatch/) or
both. It maps them to UMLS CUIs and to SNOMED CT, RxNorm, ICD-10-CM and LOINC codes, and writes
JSONL, Parquet, SQLite or OMOP `NOTE_NLP`. It runs as a library, a batch CLI, a REST service
and an MCP server.

**Alpha.** Every engine mode has been measured, but only against a gold set of 30 synthetic
notes. Claude drafted and annotated the set, and the project owner signed it off; no person
annotated it. Nothing has been validated on real notes. Treat the figures below as relative
comparisons, not accuracy.

### Extraction
- One data model (schema v1) for both engines, with three-state assertions: `negated`,
  `subject`, `history_of`, `uncertain`, `conditional`, `generic`. `None` means the engine did
  not assess the attribute. It never means `False`.
- Engine modes:

  | Mode | What it does |
  |---|---|
  | `single:umlsmatch` (default) | cTAKES-style matching and assertions |
  | `single:mmlite` | MetaMapLite matching, with NegEx or ConText (`--mmlite-negation context`) |
  | `ensemble:union` | mentions from either engine, merged, with `found_by` provenance |
  | `ensemble:consensus` | only mentions both engines found, paired by maximum one-to-one matching |
  | `ensemble:hybrid_staged` | mmlite's concepts with umlsmatch's assertion rules; `assessed_by` names the engine that set them |

  Ensembles record assertion conflicts and resolve them by `conflict_policy`.
- Configuration comes from profiles (`strict`, `clinical_recall`, `high_precision`).
  - Precedence, highest first: arguments, `CUIFLOW_*` environment variables, `cuiflow.toml`,
    profile defaults.
  - Relative paths in a config file are resolved against that file.
  - Every run records its effective settings.
  - In code, `ExtractionConfig.from_profile("high_precision", ...)` applies a profile's presets.
  - It is an error to choose a profile that would change nothing because every setting it
    presets is pinned elsewhere; the profile is not silently ignored.
- `overlaps = "longest"` keeps the longest of overlapping mentions.
- `mention_filter = "guidelines"` is off by default. It drops headings, out-of-scope concept
  types and template words. It keeps one concept per span, and the longest of overlapping
  spans.
  - A line-leading label is a heading when it is in capitals, when the colon ends its line, or
    when it is a known section or exam name ("Abdomen: soft"). A list item's label and a
    problem-list entry ("Hypertension: continue lisinopril") are content.
  - `semantic_groups` is applied before the filter, so the concept kept on a span is one of the
    wanted groups.
  - When a span has several CUIs and a terminology store is configured, a CUI with a SNOMED CT
    or RxNorm code wins. Without a store, the engine's order decides.
    `metadata["guideline_tie_break"]` records which rule was used.
- `history_of` is `None` on mentions inside a section heading. Otherwise umlsmatch would read
  "HISTORY OF PRESENT ILLNESS:" as history of its own words.
- `extract()` and `Extractor` are the library entry points. An `Extractor` closes only what it
  opened.

### Terminology
- `cuiflow build-terminology` builds `terminology.sqlite` from your own UMLS release:
  - Codes come from MRCONSO. The preferred code is the PT term if there is one, otherwise the
    term type with the best MRRANK. ICD-10-CM codes are marked as crosswalk.
  - With `--mrrel`, the store also holds:
    - the ICD-10-CM and SNOMED CT ancestor closure, walked over codes and never CUIs;
    - RxNorm product → ingredient rows.
  - On UMLS 2026AA the build takes about 3 minutes and produces 1.7 GB: 2.0M code rows, 8.4M
    hierarchy rows and 136k ingredient rows.
- `--codes`, `--ancestors N` and `--ingredients` attach codes, ancestors and ingredient RxCUIs
  to each mention. With a store built without MRREL, `--ancestors` and `--ingredients` are
  refused rather than returning empty lists.
- `TerminologyStore` provides `codes_for`, `ancestors`, `descendants`, `ingredients`,
  `cuis_for_code`, `preferred_display` and `require`.
- `cuiflow build-omop-vocab` builds an OMOP concept-ID lookup from an Athena download
  (`CONCEPT.csv`, `CONCEPT_RELATIONSHIP.csv`).

### CLI, batch runs and output
- Commands: `extract`, `export`, `evaluate`, `gold`, `build-terminology`, `build-omop-vocab`,
  `inspect-manifest`, `info`, `serve`, `mcp`.
- Inputs:
  - Accepted: text files, folders of text files, `.jsonl` and `.jsonl.gz` files, and stdin.
    Each JSONL line is `{"id": ..., "text": ...}`.
  - A folder is read for `.txt` and `.text` files. The command warns about the files it skips.
  - A UTF-8 BOM is dropped. Invalid UTF-8 becomes U+FFFD and is counted.
  - A JSONL line that cannot be read (bad JSON, or no `"text"`) is recorded as an error with
    stage `read` and key `<file>:<line>`. The run carries on.
- `extract -o` writes one file: JSONL (`.gz` too), Parquet or SQLite, with the format taken
  from the suffix.
  - The file is written as `.partial.<name>` and renamed only once complete. An existing file is
    never overwritten.
  - A document that fails is skipped and reported by exception type only, and the command
    exits 1.
- `extract --out-dir` creates a resumable run directory:
  - `--workers N` runs documents in a process pool. It needs `--out-dir`. In-flight work is
    bounded, and results keep input order.
  - A worker that dies (a crash, the OOM killer) is blamed only on the document it was
    processing. That document is recorded as `WorkerDied`, the pool restarts, and the other
    documents are retried.
  - `--timeout SECONDS` sets a per-document time limit:
    - A document that runs over is recorded as `Timeout`. The workers are then terminated and
      restarted, and the other documents are retried.
    - The clock starts when the document is next in line, so time spent queued does not count.
    - With a timeout, documents always run in worker processes, even with one worker.
  - `run_manifest.json` holds the settings, engines, terminology build, input checksums,
    timeout and each worker's peak memory. It is checkpointed every 1,000 documents.
  - Errors go to `errors.jsonl`. Each holds a document key, the stage and the exception type,
    never the message.
  - The manifest counts repeated `doc_id`s and documents with invalid UTF-8.
    `documents_in_results` counts the lines in `results.jsonl` across sessions.
- `extract --resume <run>` finishes a killed run:
  - It skips documents already done and trims a torn final line.
  - It refuses to continue if the settings, input names, engines or terminology differ from the
    first session.
  - An OS lock keeps a second session out.
- `cuiflow export` turns a run into Parquet, SQLite or OMOP CDM v5.4 `NOTE_NLP` CSV.
  - The output is written as `.partial.<name>` and renamed only once complete.
  - The export takes the run's lock, so it is refused while a batch is still writing.
  - For OMOP:
    - `note_id` comes from a file you supply (`--note-ids`), never from a file name. A run with
      a repeated `doc_id` is refused, since its notes cannot be told apart.
    - `nlp_datetime` is when the run's first session started.
    - Concept IDs come from `--omop-vocab`. A code that maps to a standard concept is
      preferred.
    - `note_nlp_id` is sequential by default, or `--id-scheme hash63`.
    - Assertions become `term_exists`, `term_temporal` and `term_modifiers`. `term_modifiers` is
      cut to 2,000 characters, the column's limit.

### Services
- `cuiflow serve`: a REST service (FastAPI, `server` extra).
  - Endpoints: `/extract`, `/concepts/{cui}`, `/info`, `/metrics` (Prometheus), and `/health`
    and `/ready` probes.
  - Extraction uses a bounded pool of extractors. When every extractor is busy, it returns 503
    with `Retry-After`.
  - `/concepts/{cui}` reads the terminology store directly, so extraction does not block
    lookups.
  - `ancestor_depth` is at most 10 and `doc_id` at most 1,000 characters (422 otherwise).
  - Metric labels carry no note text, document key or code.
- `cuiflow mcp`: an MCP server (MCP SDK 2.x, `mcp` extra) over stdio or streamable HTTP.
  - Read-only tools: `extract_clinical_concepts`, `lookup_cui`, `crosswalk_medical_code` and
    `search_medical_terms` (mmlite mode only). Extraction returns compact per-concept summaries.
  - Resources: `cuiflow://status` and `cuiflow://semantic-groups`.
  - A tool that fails returns `internal error (<exception type>)` and logs only the type. The
    message is never returned or logged, since it may quote the note.
  - `search_medical_terms` takes queries of up to 1,000 characters.
  - Warm p95 latency on UMLS 2026AA is 41 ms for `single:umlsmatch` and 36 ms for
    `single:mmlite`.
- Both services:
  - require `Authorization: Bearer <token>` when `CUIFLOW_API_TOKEN` is set;
  - refuse to listen beyond localhost without a token;
  - cap a note at 200,000 characters;
  - reject unknown code systems and out-of-range counts.

### Evaluation
- `cuiflow evaluate` scores any set of modes against a reference in one run:
  - Concepts are scored three ways: overlap (maximum one-to-one matching, the headline figure),
    exact and CUI-set.
  - Assertions are scored as agreement with the reference's labels. Coverage is reported
    separately.
  - Both sides are restricted to the same semantic groups.
  - Reference loaders: `cuiflow`, `ctakes-silver`, `mmlite-fixtures`.
- `cuiflow gold convert` builds a reference from an annotation worksheet, and `gold validate`
  checks one. `tools/` holds scripts for agreement, adjudication, blind assertion review and
  synthetic notes.
- `synthetic/` holds 100 synthetic notes of 18 types. doc_021–100 are held out for a future
  human-annotated reference.
- Results on the gold set (30 synthetic notes). The full reports are in `docs/eval/`, and
  [docs/EVALUATION.md](docs/EVALUATION.md) explains what they do and do not show.

  | Mode | Overlap precision | Recall | F1 | F1 with `guidelines` filter | Gate |
  |---|---:|---:|---:|---:|---|
  | `single:umlsmatch` | 0.218 | 0.756 | 0.338 | 0.507 | baseline |
  | `single:mmlite` | 0.104 | 0.717 | 0.181 | 0.452 | — |
  | `ensemble:consensus` | 0.337 | 0.667 | 0.447 | 0.533 | passes |
  | `ensemble:union` | 0.096 | 0.805 | 0.172 | 0.449 | fails |
  | `ensemble:hybrid_staged` | 0.104 | 0.717 | 0.181 | — | fails |

  Hybrid uses mmlite's concepts, so its concept scores equal mmlite's. The filter raises
  precision for every mode and costs 0.04–0.12 recall.

  The default stays `single:umlsmatch` until consensus is checked on real notes.
- A blind review of the assertion labels on the gold notes ([docs/GATES.md](docs/GATES.md))
  measured the precision of `True` labels:

  | Engine | Attribute | Precision of `True` labels |
  |---|---|---|
  | umlsmatch | `history_of` | 0.34 raw; about 0.55 with cuiflow's heading rule |
  | umlsmatch | `negated` | 0.81 |
  | umlsmatch | `uncertain` | 0.78 |
  | mmlite | `negated` | 0.90 |

  Do not filter on `history_of` alone.

### Examples
- `examples/notes_of_interest.py` finds the notes that affirm a concept, given as a phrase, a
  CUI, or a code and its descendants.
- `examples/note_summary.py` lists what each note affirms and denies.
- `examples/concept_matrix.py` builds a document × concept feature table.
- `examples/highlight_note.py` writes an HTML page for reviewing mentions in the note text.

### Requirements
- Python 3.11–3.14. The core has no dependencies.
- Engines, Parquet, the REST service and the MCP server are extras: `mmlite`, `umlsmatch`,
  `all-engines`, `parquet`, `server`, `mcp`, `all`.
- The spaCy model `en_core_web_sm` is not on PyPI, so it is installed separately.
- You supply the UMLS-derived data: the engine indexes and the terminology store, all built
  from the same licensed UMLS release. None of it ships with this package.

### Testing
- CI (Gitea Actions), on every push and pull request, on Python 3.11–3.14:
  - lint;
  - strict type checking;
  - the unit tests.

  The engines are covered through fakes. On a `v*` tag, CI also builds and checks the sdist
  and wheel, then publishes them to PyPI through Trusted Publishing, with attestations.
- The sdist ships `examples/` and `tools/` alongside `tests/`, so the unit tests pass from an
  unpacked sdist.
- Engine floors: umlsmatch 0.2.0, the first release with `ClinicalPipeline.assess()`, so
  `ensemble:hybrid_staged` runs from PyPI; mmlite 0.1.4.
- A nightly job runs the integration tests against the engines' latest releases. It runs on a
  self-hosted runner that holds the UMLS indexes. The job fails if the indexes are not
  configured, instead of passing with every test skipped.

### Known limitations
- Section headings are detected by a heuristic. A problem-list entry in capitals, such as
  "COPD: stable", is still read as a heading. No such entry occurs in the synthetic or gold
  notes.
- A folder input reads `.txt` and `.text` files only. Other files are skipped with a warning.
- `extract -o` runs in one process, so it has no per-document timeout. For notes that might
  hang an engine, use `--out-dir` with `--timeout`.
- `section_concept_id` in `NOTE_NLP` is always 0, because there is no section → OMOP concept
  table yet.

### Not yet done
- Human annotation of any note, and a clinician's review of the assertion labels.
- Validation on real notes under PHI controls (protocol in [docs/GATES.md](docs/GATES.md)).
