"""OMOP concept IDs from the OHDSI Standardized Vocabularies (DESIGN_PLAN §8.3).

UMLS has no OMOP ``concept_id``. This builds a small lookup database from an Athena download
(``CONCEPT.csv`` and ``CONCEPT_RELATIONSHIP.csv``, tab-separated) holding only the vocabularies
cuiflow emits codes in, plus their ``Maps to`` links to standard concepts. The Athena files have
their own licence terms; like the terminology store, the result is never distributed.

    cuiflow build-omop-vocab --athena <athena dir> --out data/omop_vocab.sqlite
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, NamedTuple

from cuiflow.core.manifests import utc_now

#: UMLS SAB -> OMOP vocabulary_id.
SAB_TO_VOCABULARY: dict[str, str] = {
    "SNOMEDCT_US": "SNOMED",
    "RXNORM": "RxNorm",
    "ICD10CM": "ICD10CM",
    "LNC": "LOINC",
}

SCHEMA = """
CREATE TABLE build_info (
    built_at TEXT NOT NULL,
    athena_dir TEXT NOT NULL,
    vocabularies TEXT NOT NULL,      -- JSON-ish "id=version;..." from VOCABULARY.csv
    cuiflow_version TEXT NOT NULL
);
CREATE TABLE concept (
    vocabulary_id TEXT NOT NULL,
    concept_code TEXT NOT NULL,
    concept_id INTEGER NOT NULL,
    standard_concept TEXT,           -- 'S', 'C' or NULL
    invalid_reason TEXT,
    PRIMARY KEY (vocabulary_id, concept_code)
);
CREATE TABLE maps_to (
    concept_id_1 INTEGER NOT NULL,
    concept_id_2 INTEGER NOT NULL,
    PRIMARY KEY (concept_id_1, concept_id_2)
);
"""


class OmopConcepts(NamedTuple):
    source_concept_id: int  # 0 when the code is not in the vocabulary
    concept_id: int  # the standard concept; 0 when there is none


def _tsv(path: Path) -> Iterator[list[str]]:
    # Athena files are tab-separated with no quoting: names may contain bare quotes.
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        header = next(reader)
        yield header
        yield from reader


def build_omop_vocabulary(
    athena_dir: Path,
    out: Path,
    *,
    vocabularies: Iterable[str] = SAB_TO_VOCABULARY.values(),
    progress: Callable[[str, int], None] | None = None,
) -> Path:
    """Build the lookup database; refuses to overwrite an existing file."""
    from cuiflow import __version__

    if out.exists():
        raise FileExistsError(f"{out} exists; remove it first or choose another path")
    wanted = frozenset(vocabularies)
    for name in ("CONCEPT.csv", "CONCEPT_RELATIONSHIP.csv"):
        if not (athena_dir / name).is_file():
            raise FileNotFoundError(f"{athena_dir / name} not found (an Athena download)")

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".partial")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
        conn.executescript(SCHEMA)
        kept: set[int] = set()

        rows = _tsv(athena_dir / "CONCEPT.csv")
        col = {name: i for i, name in enumerate(next(rows))}
        cid, voc, code = col["concept_id"], col["vocabulary_id"], col["concept_code"]
        std, inv = col["standard_concept"], col["invalid_reason"]

        def concepts() -> Iterator[tuple[Any, ...]]:
            for n, r in enumerate(rows, 1):
                if progress and n % 1_000_000 == 0:
                    progress("CONCEPT", n)
                if r[voc] in wanted:
                    kept.add(int(r[cid]))
                    yield r[voc], r[code], int(r[cid]), r[std] or None, r[inv] or None

        conn.executemany("INSERT OR IGNORE INTO concept VALUES (?, ?, ?, ?, ?)", concepts())

        rows = _tsv(athena_dir / "CONCEPT_RELATIONSHIP.csv")
        col = {name: i for i, name in enumerate(next(rows))}
        c1, c2, rel, inv = (
            col["concept_id_1"],
            col["concept_id_2"],
            col["relationship_id"],
            col["invalid_reason"],
        )

        def maps() -> Iterator[tuple[int, int]]:
            for n, r in enumerate(rows, 1):
                if progress and n % 1_000_000 == 0:
                    progress("CONCEPT_RELATIONSHIP", n)
                if r[rel] == "Maps to" and not r[inv]:
                    a = int(r[c1])
                    if a in kept:
                        yield a, int(r[c2])

        conn.executemany("INSERT OR IGNORE INTO maps_to VALUES (?, ?)", maps())

        versions = ""
        if (athena_dir / "VOCABULARY.csv").is_file():
            vrows = _tsv(athena_dir / "VOCABULARY.csv")
            vcol = {name: i for i, name in enumerate(next(vrows))}
            versions = ";".join(
                f"{r[vcol['vocabulary_id']]}={r[vcol['vocabulary_version']]}"
                for r in vrows
                if r[vcol["vocabulary_id"]] in wanted
            )
        conn.execute(
            "INSERT INTO build_info VALUES (?, ?, ?, ?)",
            (utc_now(), str(athena_dir), versions, __version__),
        )
        conn.commit()
    except BaseException:
        conn.close()
        tmp.unlink(missing_ok=True)
        raise
    conn.close()
    tmp.replace(out)
    return out


class OmopVocabulary:
    """Read-only lookups of ``(UMLS SAB, code)`` → OMOP concept IDs."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(
                f"OMOP vocabulary database not found: {self.path} "
                "(build one with: cuiflow build-omop-vocab)"
            )
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._cache: dict[tuple[str, str], OmopConcepts] = {}

    def build_info(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT built_at, athena_dir, vocabularies, cuiflow_version FROM build_info"
        ).fetchone()
        if row is None:
            return {"path": str(self.path)}
        return {
            "path": str(self.path),
            "built_at": row[0],
            "athena_dir": row[1],
            "vocabularies": dict(v.split("=", 1) for v in row[2].split(";") if "=" in v),
            "cuiflow_version": row[3],
        }

    def lookup(self, sab: str, code: str) -> OmopConcepts:
        """The code's own concept and its standard concept (itself if standard, else the
        lowest-numbered valid ``Maps to`` target). Zeros when not found."""
        key = (sab, code)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        vocabulary = SAB_TO_VOCABULARY.get(sab)
        result = OmopConcepts(0, 0)
        if vocabulary is not None:
            row = self._conn.execute(
                "SELECT concept_id, standard_concept FROM concept "
                "WHERE vocabulary_id = ? AND concept_code = ?",
                (vocabulary, code),
            ).fetchone()
            if row is not None:
                source_id, standard = row
                if standard == "S":
                    result = OmopConcepts(source_id, source_id)
                else:
                    target = self._conn.execute(
                        "SELECT MIN(concept_id_2) FROM maps_to WHERE concept_id_1 = ?",
                        (source_id,),
                    ).fetchone()[0]
                    result = OmopConcepts(source_id, target or 0)
        if len(self._cache) > 100_000:
            self._cache.clear()
        self._cache[key] = result
        return result

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> OmopVocabulary:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
