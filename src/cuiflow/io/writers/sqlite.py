"""SQLite output for local review, in the ``annotations.sqlite`` layout umlsmatch's examples use.

``documents`` and ``annotations`` follow umlsmatch's ``examples/load_to_sqlite.py``, with four
differences:

- ``negated`` is nullable. cuiflow's modes do not all assess it, and NULL means "not assessed",
  exactly as it already does for the other attributes.
- ``annotations`` adds cuiflow's own fields: ``mention_id``, ``temporality``, ``section``,
  ``found_by``, ``assessed_by`` and ``assertion_conflict``.
- ``documents.source`` is indexed but not unique: a batch whose inputs repeat a doc_id keeps
  every copy (the run manifest counts them in ``duplicate_doc_ids``).
- A ``codes`` table holds each annotation's terminology codes.

**The file is PHI**: ``source`` is the document key and ``text`` quotes the note.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cuiflow.core.models import DocumentResult

SCHEMA = """
CREATE TABLE documents (
    doc_id         INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,  -- the document key (PHI); not unique, see above
    n_annotations  INTEGER NOT NULL DEFAULT 0,
    content_sha256 TEXT,
    mode           TEXT,
    profile        TEXT
);

CREATE TABLE annotations (
    id                 INTEGER PRIMARY KEY,
    doc_id             INTEGER NOT NULL REFERENCES documents(doc_id),
    mention_id         TEXT NOT NULL,
    cui                TEXT NOT NULL,
    preferred_text     TEXT,
    semantic_group     TEXT,
    negated            INTEGER,  -- 0/1; NULL: not assessed
    subject            TEXT,     -- 'patient' | 'family_member' | 'other'
    history_of         INTEGER,
    uncertain          INTEGER,
    conditional        INTEGER,
    generic            INTEGER,
    temporality        TEXT,     -- ConText: 'recent' | 'historical' | 'hypothetical'
    start_offset       INTEGER,
    end_offset         INTEGER,
    text               TEXT,
    term               TEXT,     -- the dictionary string that matched
    section            TEXT,
    found_by           TEXT,     -- comma-separated engine names
    assessed_by        TEXT,     -- the engine that set the assertions, if not found_by's
    assertion_conflict INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE codes (
    annotation_id INTEGER NOT NULL REFERENCES annotations(id),
    system        TEXT NOT NULL,
    code          TEXT NOT NULL,
    tty           TEXT NOT NULL,
    display       TEXT NOT NULL,
    is_preferred  INTEGER NOT NULL,
    is_crosswalk  INTEGER NOT NULL
);
"""

INDEXES = """
CREATE INDEX ix_doc_source ON documents(source);
CREATE INDEX ix_ann_doc ON annotations(doc_id);
CREATE INDEX ix_ann_cui ON annotations(cui);
CREATE INDEX ix_ann_group ON annotations(semantic_group);
CREATE INDEX ix_ann_cui_neg ON annotations(cui, negated);
CREATE INDEX ix_ann_subject ON annotations(subject);
CREATE INDEX ix_ann_history ON annotations(history_of);
CREATE INDEX ix_codes_annotation ON codes(annotation_id);
CREATE INDEX ix_codes_code ON codes(system, code);
"""


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(value)


class SqliteWriter:
    def __init__(self, path: Path, *, commit_every: int = 1_000) -> None:
        if path.exists():  # never silently overwrite a previous run's output
            raise FileExistsError(f"{path} exists")
        self.path = path
        self.commit_every = commit_every
        self._pending = 0
        self._conn = sqlite3.connect(path)
        self._conn.executescript(SCHEMA)

    def write(self, result: DocumentResult) -> None:
        meta = result.metadata
        cur = self._conn.execute(
            "INSERT INTO documents (source, n_annotations, content_sha256, mode, profile) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                result.doc_id,
                len(result.mentions),
                meta.get("content_sha256"),
                meta.get("mode"),
                meta.get("profile"),
            ),
        )
        doc_rowid = cur.lastrowid
        for m in result.mentions:
            a = m.assertions
            cur = self._conn.execute(
                "INSERT INTO annotations (doc_id, mention_id, cui, preferred_text, "
                "semantic_group, negated, subject, history_of, uncertain, conditional, generic, "
                "temporality, start_offset, end_offset, text, term, section, found_by, "
                "assessed_by, assertion_conflict) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc_rowid,
                    m.mention_id,
                    m.cui,
                    m.preferred_name,
                    m.semantic_group,
                    _flag(a.negated),
                    a.subject,
                    _flag(a.history_of),
                    _flag(a.uncertain),
                    _flag(a.conditional),
                    _flag(a.generic),
                    a.temporality,
                    m.start,
                    m.end,
                    m.text,
                    m.provenance.matched_term or None,
                    m.section,
                    ",".join(m.provenance.found_by),
                    m.provenance.assessed_by,
                    int(m.provenance.assertion_conflict),
                ),
            )
            ann_id = cur.lastrowid
            self._conn.executemany(
                "INSERT INTO codes VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        ann_id,
                        c.system,
                        c.code,
                        c.tty,
                        c.display,
                        int(c.is_preferred),
                        int(c.is_crosswalk),
                    )
                    for c in m.codes
                ],
            )
        self._pending += 1
        if self._pending >= self.commit_every:
            self._conn.commit()
            self._pending = 0

    def close(self) -> None:
        self._conn.executescript(INDEXES)
        self._conn.commit()
        self._conn.close()
