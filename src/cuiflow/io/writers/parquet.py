"""Parquet output: one row per mention, nested structs for assertions, codes and provenance.

Needs the ``parquet`` extra (pyarrow). Rows are buffered and written one row group at a time,
so memory is bounded by ``row_group_size`` mentions, not by the corpus. A document with no
mentions contributes no rows; the run's ``results.jsonl`` and manifest still count it.

Assertion attributes keep their three states: a null is "not assessed", never false.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cuiflow.core.models import ASSERTION_ATTRIBUTES, DocumentResult

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "Parquet output needs the 'parquet' extra: pip install 'cuiflow[parquet]'"
    ) from exc

_BOOL_ATTRS = frozenset({"negated", "history_of", "uncertain", "conditional", "generic"})

SCHEMA = pa.schema(
    [
        ("doc_id", pa.string()),
        ("mention_id", pa.string()),
        ("start", pa.int64()),
        ("end", pa.int64()),
        ("text", pa.string()),
        ("cui", pa.string()),
        ("preferred_name", pa.string()),
        ("semantic_group", pa.string()),
        ("section", pa.string()),
        (
            "assertions",
            pa.struct(
                [(a, pa.bool_() if a in _BOOL_ATTRS else pa.string()) for a in ASSERTION_ATTRIBUTES]
            ),
        ),
        (
            "codes",
            pa.list_(
                pa.struct(
                    [
                        ("system", pa.string()),
                        ("code", pa.string()),
                        ("tty", pa.string()),
                        ("display", pa.string()),
                        ("is_preferred", pa.bool_()),
                        ("is_crosswalk", pa.bool_()),
                        ("ancestors", pa.list_(pa.string())),
                        ("ingredients", pa.list_(pa.string())),
                    ]
                )
            ),
        ),
        (
            "provenance",
            pa.struct(
                [
                    ("found_by", pa.list_(pa.string())),
                    ("semantic_types", pa.list_(pa.string())),
                    ("matched_term", pa.string()),
                    ("assertion_conflict", pa.bool_()),
                    ("assessed_by", pa.string()),
                    ("engine_assertions", pa.string()),  # JSON: its keys vary by engine
                ]
            ),
        ),
        ("mode", pa.string()),
        ("profile", pa.string()),
        ("content_sha256", pa.string()),
        ("schema_version", pa.string()),
    ]
)


class ParquetWriter:
    def __init__(self, path: Path, *, row_group_size: int = 50_000) -> None:
        if path.exists():  # never silently overwrite a previous run's output
            raise FileExistsError(f"{path} exists")
        self.path = path
        self.row_group_size = row_group_size
        self._rows: list[dict[str, Any]] = []
        self._writer = pq.ParquetWriter(path, SCHEMA, compression="zstd")

    def write(self, result: DocumentResult) -> None:
        meta = result.metadata
        for m in result.mentions:
            d = m.to_dict()
            prov = d["provenance"]
            prov["engine_assertions"] = json.dumps(prov["engine_assertions"], sort_keys=True)
            d.update(
                doc_id=result.doc_id,
                mode=meta.get("mode"),
                profile=meta.get("profile"),
                content_sha256=meta.get("content_sha256"),
                schema_version=result.schema_version,
            )
            self._rows.append(d)
        if len(self._rows) >= self.row_group_size:
            self._flush()

    def _flush(self) -> None:
        if self._rows:
            self._writer.write_table(pa.Table.from_pylist(self._rows, schema=SCHEMA))
            self._rows.clear()

    def close(self) -> None:
        self._flush()
        self._writer.close()
