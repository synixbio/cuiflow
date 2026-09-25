# Contributing to cuiflow

This file is for working on cuiflow itself. To use it, install it from PyPI as the
[README](README.md#install) describes.

## Development install

cuiflow is developed against editable checkouts of both engines, so a change to an engine can be
tested here before it is released:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e "../mmlite[nlp]" -e "../umlsmatch[nlp]"
.venv\Scripts\python -m pip install -e ".[dev,server,mcp]"
.venv\Scripts\python -m pip install en_core_web_sm@https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
```

The spaCy model is always its own step: it is not on PyPI, so no extra can depend on it.

Relative paths in this repository's instructions assume the engines and the licensed releases sit
next to it:

```
<parent>\
├── cuiflow\       this repository
├── mmlite\        engine (was metamap_api)
├── umlsmatch\     engine
├── 2026AA\META\   UMLS Metathesaurus (MRCONSO.RRF, MRRANK.RRF, MRREL.RRF, ...)
└── omop_v5.4\     Athena vocabulary download (CONCEPT.csv, CONCEPT_RELATIONSHIP.csv, ...)
```

Without the sibling checkouts, `pip install -e ".[dev,server,mcp,parquet]"` installs the engines
from PyPI instead.

## Checks

```powershell
.venv\Scripts\python -m pytest -q -m "not integration"   # unit tests: no UMLS data needed
$env:CUIFLOW_MMLITE_INDEX="data/ivf"
$env:CUIFLOW_UMLSMATCH_DB="data/umls_sno_rx.sqlite"
$env:CUIFLOW_TERMINOLOGY_DB="data/terminology.sqlite"     # built with --mrrel
.venv\Scripts\python -m pytest -q -m integration         # real engines and store
.venv\Scripts\ruff check src tests tools examples
.venv\Scripts\ruff format --check src tests tools examples
.venv\Scripts\mypy                                       # strict
```

The unit tests also pass from an unpacked sdist, which ships `tests/`, `examples/` and `tools/`.

## CI and releases

CI runs on GitHub Actions ([.github/workflows/ci.yml](.github/workflows/ci.yml)): the checks
above, without the integration tests, on Python 3.11 to 3.14. On a `v<version>` tag CI also
builds and checks the sdist and wheel, then publishes them to PyPI through Trusted Publishing.
[.gitea/workflows/ci.yml](.gitea/workflows/ci.yml) mirrors the checks for the Gitea remote but
never publishes. A nightly job ([.gitea/workflows/nightly.yml](.gitea/workflows/nightly.yml))
runs the integration tests against the engines' latest releases, on a private self-hosted Gitea
runner that holds the UMLS indexes.

The release checklist is in [RELEASING.md](RELEASING.md).
