# cuiflow

**Alpha.** The engine adapters work and are tested against real indexes. Every mode has been
measured on a gold set of 30 synthetic notes that Claude drafted, annotated and adjudicated; none
has been measured on real notes or against a human-annotated reference. See Status below and
[docs/EVALUATION.md](https://github.com/synixbio/cuiflow/blob/main/docs/EVALUATION.md).

cuiflow extracts medical concepts from free-text clinical notes and maps them to UMLS CUIs and
to SNOMED CT, RxNorm, ICD-10-CM and LOINC codes, along with clinical assertions such as negation,
subject and history. It wraps two engines behind one data model:

- [mmlite](https://pypi.org/project/mmlite/), a Python port of NLM's MetaMapLite
- [umlsmatch](https://pypi.org/project/umlsmatch/), a cTAKES-style clinical pipeline

It runs as a Python library, a batch CLI, a REST service and an MCP server, all over the same
core.

## Install

cuiflow needs Python 3.11 or later. The core has no dependencies; pick the engines and interfaces
as extras:

```powershell
pip install "cuiflow[umlsmatch]"        # default mode, single:umlsmatch
pip install "cuiflow[all-engines]"      # both engines, for the ensemble modes
pip install "cuiflow[all]"              # engines, Parquet output, REST service and MCP server
pip install en_core_web_sm@https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
```

Extras: `mmlite`, `umlsmatch`, `all-engines`, `server`, `mcp`, `parquet`, `all`, `dev`. The
spaCy model is always its own step: it is not on PyPI, so no extra can depend on it. Neither
engine works until you build its index from your own UMLS release (Data you supply, below).

The core (data model, configuration, terminology store, JSONL I/O, CLI and batch runner) uses the
standard library only.

To work on cuiflow itself, see
[CONTRIBUTING.md](https://github.com/synixbio/cuiflow/blob/main/CONTRIBUTING.md).

## Data you supply

UMLS content is licensed by the U.S. National Library of Medicine and is never distributed with
this package. You need your own UMLS licence and, **all built from the same UMLS release**:

| What | Built by | Setting | Conventional path |
|---|---|---|---|
| mmlite index | `mmlite build-index` | `mmlite_index` | `data\ivf` |
| umlsmatch dictionary | `python -m umlsmatch.build` | `umlsmatch_db` | `data\umls_sno_rx.sqlite` |
| terminology database (codes, hierarchies, ingredients) | `cuiflow build-terminology` | `terminology_db` | `data\terminology.sqlite` |
| OMOP vocabulary lookup (optional, for `NOTE_NLP` concept IDs) | `cuiflow build-omop-vocab` | `export --omop-vocab` | `data\omop_vocab.sqlite` |

Build them from the release's `META` folder (`<META>` below). Times and sizes are for UMLS 2026AA
on one Windows machine:

```powershell
mmlite -v build-index --mrconso <META>\MRCONSO.RRF --mrsty <META>\MRSTY.RRF `
    --mrsat <META>\MRSAT.RRF --out data\ivf                    # ~2 min, 2.1 GB
python -m umlsmatch.build --umls-dir <META> --db data\umls_sno_rx.sqlite   # ~3 min, 560 MB
cuiflow build-terminology --mrconso <META>\MRCONSO.RRF --mrrank <META>\MRRANK.RRF `
    --mrrel <META>\MRREL.RRF --out data\terminology.sqlite      # ~3 min, 1.7 GB
cuiflow build-omop-vocab --athena <Athena download> --out data\omop_vocab.sqlite
copy cuiflow.example.toml cuiflow.toml                         # already points at these paths
```

The mmlite index keeps every source vocabulary, and `--mrsat` adds the MeSH tree codes its MMI
scoring uses. The umlsmatch build runs its three stages in order (dictionary, re-tokenizing with
spaCy, rare-word index), so it needs `en_core_web_sm` installed. Its defaults keep the concepts
that have an RxNorm or SNOMED CT code and fall in cTAKES's semantic types.

Without `--mrrel` the store holds codes only. With it, it also holds every ICD-10-CM and SNOMED CT
code's ancestors (walked over codes, never CUIs: DESIGN_PLAN §5.2) and each RxNorm product's
ingredients. The OMOP vocabulary comes from [Athena](https://athena.ohdsi.org), under its own
licence terms.

## Use it

```powershell
cuiflow extract note.txt                                  # JSONL to stdout
cuiflow extract notes.jsonl.gz                            # {"id": ..., "text": ...} per line
cuiflow extract notes\ --codes SNOMEDCT_US,RXNORM --terminology-db data\terminology.sqlite
cuiflow extract notes\ --codes ICD10CM,RXNORM --ancestors 3 --ingredients ...   # + hierarchy
cuiflow extract notes\ -o mentions.parquet                # or .sqlite, .jsonl[.gz]
cuiflow extract notes\ --out-dir out\runs --workers 4     # run directory + run_manifest.json
cuiflow extract notes\ --out-dir out\runs --timeout 60    # a document over 60 s is a Timeout error
cuiflow extract notes\ --resume out\runs\<run-id>         # finish a killed run: same inputs, typed the same
cuiflow export out\runs\<run-id> -o mentions.parquet      # or .sqlite
cuiflow export out\runs\<run-id> -o note_nlp.csv --note-ids ids.csv --omop-vocab data\omop_vocab.sqlite
cuiflow info --load                                       # effective config + loaded indexes
cuiflow serve --port 8000                                 # REST; OpenAPI docs at /docs, /metrics
cuiflow mcp                                               # MCP over stdio
cuiflow mcp --transport http --port 8765                  # MCP over HTTP (set CUIFLOW_API_TOKEN)
```

```python
from cuiflow import Extractor, load_config

with Extractor(load_config({"mode": "single:umlsmatch"})) as ex:
    result = ex.process("Patient denies chest pain.", "note-1")
    for m in result.mentions:
        print(m.cui, m.text, m.semantic_group, m.assertions.negated, m.assertions.history_of)
```

**An assertion that was not assessed is `None`, never `False`.** mmlite never assesses
`history_of`, for example, so it is `None` on every mmlite mention. Branch on `None`; do not
coerce it.

**umlsmatch's `history_of` is often wrong.** In a blind review of its labels on the 30 gold notes
([docs/GATES.md](https://github.com/synixbio/cuiflow/blob/main/docs/GATES.md)), only 0.34 of its `history_of = True` mentions were history
(95% CI 0.24–0.46). Most of the wrong ones were the words of the heading "HISTORY OF PRESENT
ILLNESS"; cuiflow now marks `history_of` as not assessed inside any heading, which takes
precision on the reviewed labels to about 0.55. The rest are events of this encounter and
durations read as history. Do not filter on `history_of` alone.

[examples/](https://github.com/synixbio/cuiflow/blob/main/examples/README.md) builds on this: `notes_of_interest.py` finds the notes that affirm
a concept (not negated, about the patient, not hedged), by phrase, CUI, or code plus everything
below it. `note_summary.py` lists what each note affirms and denies, `concept_matrix.py` builds a
document x concept feature table, and `highlight_note.py` writes an HTML page for reviewing
mentions in the note text.

### Configuration

Precedence, highest first: CLI flags > `CUIFLOW_*` environment variables > `cuiflow.toml` >
profile defaults. Copy [cuiflow.example.toml](https://github.com/synixbio/cuiflow/blob/main/cuiflow.example.toml) to start; relative paths in
it are relative to the file, and `./cuiflow.toml` is read by library calls too. Every run
manifest records the *effective* settings.

| Mode | What it does | Status |
|---|---|---|
| `single:umlsmatch` (default) | cTAKES-style matching; `negated`, `subject`, `history_of`, `uncertain` | adapter verified |
| `single:mmlite` | MetaMapLite matching; NegEx, or ConText with `--mmlite-negation context` | adapter verified |
| `ensemble:union` | mentions from either engine, merged, with `found_by` provenance | **fails its gate** on the gold set (docs/EVALUATION.md) |
| `ensemble:consensus` | only mentions both engines found | **passes its gate** on the synthetic gold set: higher precision, lower recall; experimental until checked on real notes (docs/EVALUATION.md) |
| `ensemble:hybrid_staged` | mmlite concepts, umlsmatch assertions | **fails its gate** on the gold set |

On the gold set (30 synthetic notes, Claude-drafted and signed off by the project owner; it is
kept outside this repository), consensus is the one ensemble that passes: precision 0.337 vs 0.218 for umlsmatch, F1 0.447 vs 0.338, recall
0.667 vs 0.756. Union gains a little recall and loses far more precision (mostly mmlite's broad
scope, e.g. several CUIs for "Patient"). The gate is relative: against this reference, about
two of every three consensus mentions are not in the gold set. None of this has been checked on
real notes, and parts of the gold set were annotated after its first scores were seen
([docs/EVALUATION.md](https://github.com/synixbio/cuiflow/blob/main/docs/EVALUATION.md#what-the-gold-set-results-do-and-do-not-show)).

## Working with PHI

Every output (results, manifests, logs) quotes note text or names source files, and file names
are often patient identifiers. Treat them all as PHI. `out/`, `runs/`, `data/` and result files
are git-ignored. Error records hold only a document key, the exception type and the stage,
never the exception message, because parser errors often quote the input.

The REST service requires `Authorization: Bearer <token>` when `CUIFLOW_API_TOKEN` is set,
on everything but the `/health` and `/ready` probes, including `/metrics`. `cuiflow serve` and
`cuiflow mcp --transport http` refuse to listen beyond localhost without a token, and both
interfaces accept only `Bearer <token>`, not the bare token. Both cap a note at 200,000
characters; the MCP tools also refuse unknown code systems and counts out of bounds. No metric
label carries note text, a document key or a code. An MCP tool that fails returns only the
exception type, never its message. The MCP server keeps extraction local, but
a note an agent sends it is already in that agent's conversation and has gone to its model
provider.

## Status

| Area | State |
|---|---|
| Data model, config, manifests, CLI, batch runner | working, unit-tested |
| mmlite and umlsmatch adapters | working, integration-tested on UMLS 2026AA indexes |
| Ensembles (union, consensus, hybrid-staged) | working; on the synthetic gold set consensus passes its gate, union and hybrid-staged fail |
| Terminology store: codes and preferred-code rule | working, unit-tested |
| Terminology store: hierarchies, RxNorm ingredients | working; Phase 2 gate passes on UMLS 2026AA ([docs/GATES.md](https://github.com/synixbio/cuiflow/blob/main/docs/GATES.md)) |
| REST service (Prometheus `/metrics`), MCP server | working; every MCP tool tested over stdio; latency gate passes |
| Parquet, SQLite and OMOP `NOTE_NLP` output (`cuiflow export`) | working; Phase 4 gate evidence in [docs/GATES.md](https://github.com/synixbio/cuiflow/blob/main/docs/GATES.md) |
| Evaluation harness (`cuiflow evaluate`) | working; baseline in [docs/EVALUATION.md](https://github.com/synixbio/cuiflow/blob/main/docs/EVALUATION.md) |
| Gold-set tooling (`cuiflow gold convert` / `validate`) | ready; the guidelines and gold sets are kept outside this repository |
| Human-annotated gold set | not yet: needs notes and annotators |

## Development

The development install, the checks, CI and the release process are in
[CONTRIBUTING.md](https://github.com/synixbio/cuiflow/blob/main/CONTRIBUTING.md).

## Licence

Apache-2.0. UMLS content is licensed separately by the NLM; SNOMED CT carries additional
affiliate terms outside the US.
