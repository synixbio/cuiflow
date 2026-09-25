"""Real engines on a built index. Skipped unless the index paths are set:

    CUIFLOW_MMLITE_INDEX=data/ivf
    CUIFLOW_UMLSMATCH_DB=data/umls_sno_rx.sqlite

The sentences are invented. Assertions are deliberately loose: these check that each adapter
maps its engine's output correctly, not the engines' accuracy (that is the Phase 1 harness).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cuiflow.api import Extractor
from cuiflow.core.config import ExtractionConfig
from cuiflow.core.enums import EngineMode

pytestmark = pytest.mark.integration

MMLITE_INDEX = os.environ.get("CUIFLOW_MMLITE_INDEX")
UMLSMATCH_DB = os.environ.get("CUIFLOW_UMLSMATCH_DB")
needs_mmlite = pytest.mark.skipif(not MMLITE_INDEX, reason="CUIFLOW_MMLITE_INDEX not set")
needs_umlsmatch = pytest.mark.skipif(not UMLSMATCH_DB, reason="CUIFLOW_UMLSMATCH_DB not set")

NOTE = "Patient denies chest pain. History of diabetes mellitus, on metformin."
CHEST_PAIN, METFORMIN = "C0008031", "C0025598"


def _config(mode: EngineMode, **kw: object) -> ExtractionConfig:
    return ExtractionConfig(
        mode=mode,
        mmlite_index=Path(MMLITE_INDEX) if MMLITE_INDEX else None,
        umlsmatch_db=Path(UMLSMATCH_DB) if UMLSMATCH_DB else None,
        **kw,  # type: ignore[arg-type]
    )


def _check_spans(result_text: str, mentions: object) -> None:
    for m in mentions:  # type: ignore[attr-defined]
        assert result_text[m.start : m.end] == m.text


@needs_mmlite
@pytest.mark.parametrize("negation", ["negex", "context"])
def test_mmlite_adapter(negation: str) -> None:
    with Extractor(_config(EngineMode.MMLITE, mmlite_negation=negation)) as ex:
        result = ex.process(NOTE, "n")
        info = ex.info()
    assert info["engine"]["name"] == "mmlite"
    by_cui = {m.cui: m for m in result.mentions}
    assert CHEST_PAIN in by_cui and METFORMIN in by_cui
    chest = by_cui[CHEST_PAIN]
    assert chest.assertions.negated is True
    assert chest.provenance.semantic_types and chest.semantic_group != "UNKNOWN"
    assert by_cui[METFORMIN].semantic_group == "DRUG"
    assert chest.assertions.history_of is None  # mmlite never assesses it
    if negation == "negex":
        assert chest.assertions.subject is None  # only ConText assesses experiencer
    else:
        assert chest.assertions.subject == "patient"
    _check_spans(NOTE, result.mentions)


@needs_umlsmatch
def test_umlsmatch_adapter() -> None:
    with Extractor(_config(EngineMode.UMLSMATCH)) as ex:
        result = ex.process(NOTE, "n")
    by_cui = {m.cui: m for m in result.mentions}
    assert by_cui[CHEST_PAIN].assertions.negated is True
    assert by_cui[CHEST_PAIN].assertions.subject == "patient"
    assert by_cui[METFORMIN].semantic_group == "DRUG"
    assert by_cui[METFORMIN].assertions.generic is None  # never assessed
    _check_spans(NOTE, result.mentions)


@needs_mmlite
@needs_umlsmatch
def test_union_combines_both() -> None:
    with Extractor(_config(EngineMode.UNION)) as ex:
        result = ex.process(NOTE, "n")
    chest = next(m for m in result.mentions if m.cui == CHEST_PAIN)
    assert chest.provenance.found_by == ("mmlite", "umlsmatch")
    _check_spans(NOTE, result.mentions)


def _umlsmatch_has_assess() -> bool:
    try:
        from umlsmatch.analyze import ClinicalPipeline
    except ImportError:
        return False
    return callable(getattr(ClinicalPipeline, "assess", None))


@needs_mmlite
@needs_umlsmatch
@pytest.mark.skipif(not _umlsmatch_has_assess(), reason="umlsmatch lacks assess(); needs 0.2.0+")
def test_hybrid_staged() -> None:
    with Extractor(_config(EngineMode.HYBRID)) as ex:
        result = ex.process(NOTE, "n")
    by_cui = {m.cui: m for m in result.mentions}
    chest = by_cui[CHEST_PAIN]
    assert chest.provenance.found_by == ("mmlite",)  # concepts from mmlite
    assert chest.assertions.negated is True  # ... assertions from umlsmatch's rules,
    assert chest.assertions.subject == "patient"  # which mmlite (NegEx) never assesses
    assert chest.provenance.engine_assertions["mmlite"]["subject"] is None
    _check_spans(NOTE, result.mentions)


# ---- Phase 1 gate: each adapter's output equals its engine's own, converted ----------------

ROOT = Path(__file__).resolve().parents[2]
# The 30 gold notes, verbatim. The gold set is kept outside the repository: point CUIFLOW_GOLD at
# your copy of gold.jsonl to include them.
GOLD_NOTES = Path(os.environ.get("CUIFLOW_GOLD", ROOT / "gold.jsonl"))


def _notes() -> list[tuple[str, str]]:
    """The gold notes, plus the notes in synthetic/."""
    notes = []
    if GOLD_NOTES.is_file():
        with GOLD_NOTES.open(encoding="utf-8") as fh:
            notes += [(r["doc_id"], r["text"]) for r in map(json.loads, fh) if r]
    notes += [(p.name, p.read_text(encoding="utf-8")) for p in sorted(ROOT.glob("synthetic/*.txt"))]
    if not notes:  # an sdist ships tests/ but not docs/ or synthetic/
        pytest.skip("no notes found: set CUIFLOW_GOLD, or add synthetic/*.txt")
    return notes


@needs_mmlite
def test_mmlite_adapter_matches_the_engine_run_directly() -> None:
    import dataclasses

    from mmlite import MetaMapLite
    from mmlite.config import Settings

    settings = dataclasses.replace(
        Settings.load(index_directory=Path(MMLITE_INDEX or "")), negation_detector="negex"
    )
    direct = MetaMapLite(settings, remove_subsumed=True)
    notes = _notes()
    assert len(notes) >= 20
    try:
        with Extractor(_config(EngineMode.MMLITE)) as ex:
            for doc_id, text in notes:
                expected = sorted(
                    {
                        (e.start, e.start + e.length, ev.concept.cui, bool(e.negated))
                        for e in direct.process_text(text, fieldid=None)
                        for ev in e.evs
                    }
                )
                got = sorted(
                    (m.start, m.end, m.cui, m.assertions.negated)
                    for m in ex.process(text, doc_id).mentions
                )
                assert got == expected, doc_id
    finally:
        direct.close()


@needs_umlsmatch
def test_umlsmatch_adapter_matches_the_engine_run_directly() -> None:
    from umlsmatch.analyze import ClinicalPipeline

    direct = ClinicalPipeline(UMLSMATCH_DB, profile="strict", resolve_overlaps=False)
    notes = _notes()
    try:
        with Extractor(_config(EngineMode.UMLSMATCH)) as ex:
            for doc_id, text in notes:
                expected = sorted(
                    (a.start, a.end, a.cui, a.group, a.negated, a.subject, a.history_of,
                     a.uncertain, a.conditional, a.generic)
                    for a in direct.analyze(text)
                )  # fmt: skip
                got = sorted(
                    (m.start, m.end, m.cui, m.semantic_group, m.assertions.negated,
                     m.assertions.subject, m.assertions.history_of, m.assertions.uncertain,
                     m.assertions.conditional, m.assertions.generic)
                    # The adapter, not process(): that also un-assesses history on headings.
                    for m in ex.engine.extract(text, doc_id)
                )  # fmt: skip
                assert got == expected, doc_id
    finally:
        direct.close()


@needs_umlsmatch
def test_worker_processes_with_a_timeout(tmp_path: Path) -> None:
    from cuiflow.interfaces.batch import RESULTS_NAME, run_batch
    from cuiflow.io.writers.jsonl import read_jsonl

    notes = tmp_path / "notes"
    notes.mkdir()
    for i in range(4):
        (notes / f"n{i}.txt").write_text(NOTE, encoding="utf-8")
    run_dir = run_batch(
        [notes], tmp_path / "runs", _config(EngineMode.UMLSMATCH), workers=2, timeout=120
    )
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["documents"] == 4 and manifest["errors"] == []
    assert manifest["timeout_seconds"] == 120 and manifest["worker_peak_rss_mb"]
    results = read_jsonl(run_dir / RESULTS_NAME)
    assert all(CHEST_PAIN in {m.cui for m in r.mentions} for r in results)
