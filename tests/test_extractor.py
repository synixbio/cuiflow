from __future__ import annotations

from pathlib import Path

import pytest

from cuiflow.api import DocumentInput, Extractor
from cuiflow.core.config import ExtractionConfig
from cuiflow.core.enums import EngineMode, OverlapPolicy
from cuiflow.terminology.store import TerminologyStore
from tests.conftest import FakeEngine

NOTE = "No chest pain. Diabetes on metformin."


def test_process_basic(fake_engine: FakeEngine) -> None:
    with Extractor(ExtractionConfig(), engine=fake_engine) as ex:
        result = ex.process(NOTE, "n1")
    assert not fake_engine.closed  # injected: the caller's to close
    by_cui = {m.cui: m for m in result.mentions}
    assert by_cui["C0008031"].assertions.negated is True
    assert by_cui["C0025598"].assertions.negated is False
    assert result.text is None  # PHI is opt-in
    assert result.metadata["mode"] == "single:umlsmatch"
    assert len(result.metadata["content_sha256"]) == 64


def test_group_filter_and_include_text(fake_engine: FakeEngine) -> None:
    cfg = ExtractionConfig(semantic_groups=("drug",), include_text=True)
    result = Extractor(cfg, engine=fake_engine).process(NOTE)
    assert [m.cui for m in result.mentions] == ["C0025598"]
    assert result.text == NOTE


def test_overlaps_longest_applied_outside_umlsmatch(fake_engine: FakeEngine) -> None:
    keep = Extractor(ExtractionConfig(mode=EngineMode.MMLITE), engine=fake_engine).process(NOTE)
    assert {"C0008031", "C0030193"} <= {m.cui for m in keep.mentions}
    cfg = ExtractionConfig(mode=EngineMode.MMLITE, overlaps=OverlapPolicy.LONGEST)
    longest = Extractor(cfg, engine=fake_engine).process(NOTE)
    assert "C0030193" not in {m.cui for m in longest.mentions}  # "pain" inside "chest pain"


def test_codes_attached_from_store(fake_engine: FakeEngine, terminology_db: Path) -> None:
    cfg = ExtractionConfig(code_systems=("SNOMEDCT_US", "RXNORM"), terminology_db=terminology_db)
    with Extractor(cfg, engine=fake_engine) as ex:
        result = ex.process(NOTE)
        assert ex.info()["terminology"]["umls_release"] == "2026AA"
    codes = {m.cui: {(c.system, c.code) for c in m.codes} for m in result.mentions}
    assert ("SNOMEDCT_US", "29857009") in codes["C0008031"]
    assert codes["C0025598"] == {("RXNORM", "6809")}


def test_code_systems_without_store_is_an_error(fake_engine: FakeEngine) -> None:
    with pytest.raises(ValueError, match="terminology"):
        Extractor(ExtractionConfig(code_systems=("RXNORM",)), engine=fake_engine)


def test_engine_failure_closes_only_the_store_it_opened(
    terminology_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cuiflow.api

    closed: list[TerminologyStore] = []
    real_close = TerminologyStore.close

    def close(self: TerminologyStore) -> None:
        closed.append(self)
        real_close(self)

    def broken(config: ExtractionConfig) -> FakeEngine:
        raise ImportError("engine extra missing")

    monkeypatch.setattr(TerminologyStore, "close", close)
    monkeypatch.setattr(cuiflow.api, "create_engine", broken)
    with pytest.raises(ImportError):
        Extractor(ExtractionConfig(terminology_db=terminology_db))
    assert len(closed) == 1  # the store it opened, not left to the garbage collector

    given = TerminologyStore(terminology_db)
    with pytest.raises(ImportError):
        Extractor(ExtractionConfig(), store=given)
    assert len(closed) == 1  # the caller's store is the caller's to close
    given.close()


def test_run_assigns_doc_ids(fake_engine: FakeEngine, terminology_db: Path) -> None:
    ex = Extractor(ExtractionConfig(), engine=fake_engine, store=TerminologyStore(terminology_db))
    ids = [r.doc_id for r in ex.run(["a", DocumentInput("mine", "b"), "c"])]
    assert ids == ["doc-000001", "mine", "doc-000003"]


def test_close_closes_owned_store_even_if_engine_close_fails(
    terminology_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cuiflow.api

    class Broken(FakeEngine):
        def close(self) -> None:
            raise RuntimeError("engine close failed")

    monkeypatch.setattr(cuiflow.api, "create_engine", lambda config: Broken())
    ex = Extractor(ExtractionConfig(terminology_db=terminology_db))
    assert ex.store is not None
    with pytest.raises(RuntimeError, match="engine close failed"):
        ex.close()
    with pytest.raises(Exception, match="closed"):
        ex.store._conn.execute("SELECT 1")  # the store it opened was still closed


def test_groups_are_filtered_before_one_concept_per_span() -> None:
    import dataclasses

    class TwoReadings(FakeEngine):
        def extract(self, text: str, doc_id: str):  # type: ignore[no-untyped-def]
            (m,) = [m for m in super().extract(text, doc_id) if m.cui == "C0025598"]
            # The engine ranks an out-of-scope reading of the same span first.
            other = dataclasses.replace(m, cui="C9999999", semantic_group="FINDING")
            return [other, m]

    cfg = ExtractionConfig(semantic_groups=("DRUG",), mention_filter="guidelines")
    result = Extractor(cfg, engine=TwoReadings()).process("Takes metformin.")
    assert [m.cui for m in result.mentions] == ["C0025598"]  # not an emptied span
    assert result.metadata["guideline_tie_break"] == "engine_order"  # no terminology store
