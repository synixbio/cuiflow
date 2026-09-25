from __future__ import annotations

import pytest

from cuiflow.core.enums import ConflictPolicy, EngineMode
from cuiflow.core.models import AssertionStatus, Mention, ProvenanceInfo, make_mention_id
from cuiflow.engines.ensemble import (
    EnsembleEngine,
    HybridEngine,
    consensus,
    merge_assertions,
    union,
)
from tests.conftest import FakeAssessor, FakeEngine

TEXT = "No chest pain, on metformin."


def m(engine: str, start: int, end: int, cui: str, **assertions: object) -> Mention:
    return Mention(
        mention_id=make_mention_id("d", start, end, cui),
        start=start,
        end=end,
        text=TEXT[start:end],
        cui=cui,
        preferred_name=cui,
        semantic_group="FINDING",
        assertions=AssertionStatus(**assertions),  # type: ignore[arg-type]
        provenance=ProvenanceInfo(found_by=(engine,)),
    )


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (ConflictPolicy.PREFER_UMLSMATCH, True),
        (ConflictPolicy.PREFER_MMLITE, False),
        (ConflictPolicy.NEGATED_IF_ANY, True),
        (ConflictPolicy.UNKNOWN, None),
    ],
)
def test_merge_assertions_conflict(policy: ConflictPolicy, expected: bool | None) -> None:
    combined, conflict, per_engine = merge_assertions(
        {
            "mmlite": AssertionStatus(negated=False),
            "umlsmatch": AssertionStatus(negated=True, history_of=False),
        },
        policy,
    )
    assert conflict is True
    assert combined.negated is expected
    assert combined.history_of is False  # only one engine assessed it: no conflict, kept
    assert per_engine["mmlite"]["negated"] is False


def test_merge_assertions_agreement_records_nothing() -> None:
    combined, conflict, per_engine = merge_assertions(
        {"mmlite": AssertionStatus(negated=True), "umlsmatch": AssertionStatus(negated=True)},
        ConflictPolicy.PREFER_UMLSMATCH,
    )
    assert combined.negated is True and not conflict and per_engine == {}


def test_union_merges_spans_that_differ_by_punctuation() -> None:
    by_engine = {
        "mmlite": [m("mmlite", 3, 14, "C0008031", negated=True)],  # "chest pain,"
        "umlsmatch": [
            m("umlsmatch", 3, 13, "C0008031", negated=True),  # "chest pain"
            m("umlsmatch", 18, 27, "C0025598", negated=False),
        ],
    }
    out = union(TEXT, by_engine, "d", ConflictPolicy.PREFER_UMLSMATCH)
    assert [(x.cui, x.provenance.found_by) for x in out] == [
        ("C0008031", ("mmlite", "umlsmatch")),
        ("C0025598", ("umlsmatch",)),
    ]
    assert (out[0].start, out[0].end) == (3, 13)  # the preferred engine's span


def test_consensus_needs_both_engines() -> None:
    by_engine = {
        "mmlite": [m("mmlite", 3, 8, "C0008031")],  # overlapping, same CUI
        "umlsmatch": [m("umlsmatch", 3, 13, "C0008031"), m("umlsmatch", 18, 27, "C0025598")],
    }
    out = consensus(TEXT, by_engine, "d", ConflictPolicy.PREFER_UMLSMATCH)
    assert [x.cui for x in out] == ["C0008031"]


def test_consensus_pairs_as_many_as_possible() -> None:
    # "chest pain" and "pain" from umlsmatch; "chest pain" and "chest" from mmlite, same CUI.
    # First-come pairing spends umlsmatch's "chest pain" on mmlite's "chest pain", leaving
    # "pain" with no partner. A maximum matching pairs both.
    by_engine = {
        "umlsmatch": [m("umlsmatch", 3, 13, "C0008031"), m("umlsmatch", 9, 13, "C0008031")],
        "mmlite": [m("mmlite", 3, 13, "C0008031"), m("mmlite", 3, 8, "C0008031")],
    }
    out = consensus(TEXT, by_engine, "d", ConflictPolicy.PREFER_UMLSMATCH)
    assert [(x.start, x.end) for x in out] == [(3, 13), (9, 13)]
    assert all(x.provenance.found_by == ("mmlite", "umlsmatch") for x in out)


def test_hybrid_is_not_an_ensemble_engine() -> None:
    with pytest.raises(ValueError, match="HybridEngine"):
        EnsembleEngine({}, EngineMode.HYBRID)


def test_hybrid_needs_assess_support() -> None:
    with pytest.raises(NotImplementedError, match=r"0\.2\.0 or later"):
        HybridEngine(FakeEngine("mmlite"), FakeEngine("umlsmatch"))  # type: ignore[arg-type]


def test_hybrid_takes_concepts_from_one_and_assertions_from_the_other() -> None:
    concepts = FakeEngine("mmlite")
    engine = HybridEngine(concepts, FakeAssessor("umlsmatch"))
    text = "No chest pain." + " " * 100 + "metformin"
    out = {m.cui: m for m in engine.extract(text, "d")}
    chest, metformin = out["C0008031"], out["C0025598"]
    # The assessor's answers replace the concept engine's (which said negated=True) ...
    assert chest.assertions == AssertionStatus(negated=False, history_of=True)
    assert chest.provenance.found_by == ("mmlite",)
    assert chest.provenance.assessed_by == "umlsmatch"  # whose rules the assertions are
    # ... which stay visible for comparison.
    assert chest.provenance.engine_assertions["mmlite"]["negated"] is True
    # A span the assessor could not assess is "not assessed", not the concept engine's guess.
    assert metformin.assertions == AssertionStatus()
    engine.close()
    assert concepts.closed


def test_ensemble_engine_runs_both() -> None:
    engine = EnsembleEngine(
        {"mmlite": FakeEngine("mmlite"), "umlsmatch": FakeEngine("umlsmatch")}, EngineMode.UNION
    )
    out = engine.extract("No chest pain.", "d")
    assert {x.provenance.found_by for x in out} == {("mmlite", "umlsmatch")}


def test_close_closes_every_engine_when_one_fails() -> None:
    class BadClose(FakeEngine):
        def close(self) -> None:
            super().close()
            raise OSError("index file busy")

    first, second = BadClose("mmlite"), FakeEngine("umlsmatch")
    with pytest.raises(OSError, match="busy"):
        EnsembleEngine({"mmlite": first, "umlsmatch": second}, EngineMode.UNION).close()
    assert first.closed and second.closed

    concepts, assessor = BadClose("mmlite"), FakeAssessor("umlsmatch")
    with pytest.raises(OSError, match="busy"):
        HybridEngine(concepts, assessor).close()
    assert concepts.closed and assessor.closed
