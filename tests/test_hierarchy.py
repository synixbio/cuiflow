"""Code hierarchy and RxNorm ingredients (DESIGN_PLAN §5.2 rules 2-5), on synthetic RRF rows.

Ported from ``metamap_api/tests/examples/test_add_{icd10cm_hierarchy,snomedct_hierarchy,
rxnorm_ingredients}.py``. Each fixture reproduces the shape of a real UMLS 2026AA case that
those scripts were fixed against; the real cases themselves are checked in
``tests/integration/test_terminology_gate.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from cuiflow.api import Extractor
from cuiflow.core.config import ExtractionConfig
from cuiflow.terminology.builder import build_terminology, read_relations
from cuiflow.terminology.hierarchy import code_ancestors
from cuiflow.terminology.ingredients import resolve_ingredients
from cuiflow.terminology.store import TerminologyStore
from tests.conftest import FakeEngine

# CUI|LAT|TS|LUI|STT|SUI|ISPREF|AUI|SAUI|SCUI|SDUI|SAB|TTY|CODE|STR|SRL|SUPPRESS|CVF
MRCONSO = [
    # ICD-10-CM: T01 -> T01.1 -> T01.10, and T01.10 also under T02 (the DAG case). D9000002 owns
    # the category T01.1 and the leaf T01.19, like C1260922 owns R06 and R06.9; only T01.1 is on
    # T01.10's path, which a CUI-level walk cannot express.
    "D9000001|ENG|P|L1|PF|S1|Y|A1|||T01|ICD10CM|HT|T01|Top Category|4|N||",
    "D9000002|ENG|S|L2|VO|S2|Y|A2|||T01.1|ICD10CM|HT|T01.1|Mid Category|4|N||",
    "D9000002|ENG|S|L5|PF|S5|Y|A5|||T01.19|ICD10CM|PT|T01.19|Mid Category, unspecified|4|N||",
    "D9000003|ENG|P|L3|PF|S3|Y|A3|||T01.10|ICD10CM|PT|T01.10|Leaf Code|4|N||",
    "D9000004|ENG|P|L4|PF|S4|Y|A4|||T02|ICD10CM|HT|T02|Other Top Category|4|N||",
    # SNOMED CT: 100 -> 200 -> 300, 300 also under 400. S9000003 owns a second code 350 whose
    # only parent is 500, like lisinopril's substance and product codes.
    "S9000001|ENG|P|L1|PF|S1|Y|B1|||100|SNOMEDCT_US|PT|100|Top Concept|9|N||",
    "S9000002|ENG|P|L2|PF|S2|Y|B2|||200|SNOMEDCT_US|PT|200|Mid Concept|9|N||",
    "S9000003|ENG|P|L3|PF|S3|Y|B3|||300|SNOMEDCT_US|PT|300|Leaf Concept|9|N||",
    "S9000003|ENG|S|L6|PF|S6|Y|B6|||350|SNOMEDCT_US|PT|350|Leaf Concept (product)|9|N||",
    "S9000004|ENG|P|L4|PF|S4|Y|B4|||400|SNOMEDCT_US|PT|400|Other Top Concept|9|N||",
    "S9000005|ENG|P|L5|PF|S5|Y|B5|||500|SNOMEDCT_US|PT|500|Product Top Concept|9|N||",
    # RxNorm: IN <- SCDC <- SCD <- SBD, an SBD -> BN edge whose BN resolves onward (like the
    # real Zestril BN), a dose form with no relations, and an SBD whose BN is a dead end.
    "C9000001|ENG|P|L1|PF|S1|Y|X1|||29999|RXNORM|IN|29999|testdrug|0|N||",
    "C9000002|ENG|P|L2|PF|S2|Y|X2|||39999|RXNORM|SCDC|39999|testdrug 10 MG|0|N||",
    "C9000003|ENG|P|L3|PF|S3|Y|X3|||49999|RXNORM|SCD|49999|testdrug 10 MG Oral Tablet|0|N||",
    "C9000004|ENG|P|L4|PF|S4|Y|X4|||59999|RXNORM|SBD|59999|testdrug 10 MG [TestBrand]|0|N||",
    "C9000005|ENG|P|L5|PF|S5|Y|X5|||69999|RXNORM|BN|69999|TestBrand|0|N||",
    "C9000006|ENG|P|L6|PF|S6|Y|X6|||79999|RXNORM|DF|79999|Oral Tablet|0|N||",
    "C9000007|ENG|P|L7|PF|S7|Y|X7|||89999|RXNORM|SBD|89999|otherdrug 5 MG [DeadEnd]|0|N||",
    "C9000008|ENG|P|L8|PF|S8|Y|X8|||99999|RXNORM|BN|99999|DeadEnd|0|N||",
]

# CUI1|AUI1|STYPE1|REL|CUI2|AUI2|STYPE2|RELA|RUI|SRUI|SAB|SL|RG|DIR|SUPPRESS|CVF
MRREL = [
    # CHD: parent in AUI1, child in AUI2.
    "D9000001|A1|SDUI|CHD|D9000002|A2|SDUI||R1||ICD10CM|ICD10CM|||N||",
    "D9000002|A2|SDUI|CHD|D9000002|A5|SDUI||R4||ICD10CM|ICD10CM|||N||",
    "D9000002|A2|SDUI|CHD|D9000003|A3|SDUI||R2||ICD10CM|ICD10CM|||N||",
    "D9000004|A4|SDUI|CHD|D9000003|A3|SDUI||R3||ICD10CM|ICD10CM|||N||",
    "S9000001|B1|SCUI|CHD|S9000002|B2|SCUI|isa|R5||SNOMEDCT_US|SNOMEDCT_US|||N||",
    "S9000002|B2|SCUI|CHD|S9000003|B3|SCUI|isa|R6||SNOMEDCT_US|SNOMEDCT_US|||N||",
    "S9000004|B4|SCUI|CHD|S9000003|B3|SCUI|isa|R7||SNOMEDCT_US|SNOMEDCT_US|||N||",
    "S9000005|B5|SCUI|CHD|S9000003|B6|SCUI|isa|R8||SNOMEDCT_US|SNOMEDCT_US|||N||",
    # A suppressed (obsolete) edge: ignored by default.
    "S9000005|B5|SCUI|CHD|S9000001|B1|SCUI|isa|R9||SNOMEDCT_US|SNOMEDCT_US|||O||",
    # "CUI2 RELA CUI1":
    "C9000001|X1|SCUI|RO|C9000002|X2|SCUI|has_ingredient|R10||RXNORM|RXNORM|||N||",
    "C9000003|X3|SCUI|RN|C9000004|X4|SCUI|tradename_of|R11||RXNORM|RXNORM|||N||",
    "C9000002|X2|SCUI|RO|C9000003|X3|SCUI|consists_of|R12||RXNORM|RXNORM|||N||",
    "C9000002|X2|SCUI|RO|C9000004|X4|SCUI|consists_of|R13||RXNORM|RXNORM|||N||",
    "C9000005|X5|SCUI|RO|C9000004|X4|SCUI|has_ingredient|R14||RXNORM|RXNORM|||N||",
    "C9000001|X1|SCUI|RN|C9000005|X5|SCUI|tradename_of|R15||RXNORM|RXNORM|||N||",
    "C9000008|X8|SCUI|RO|C9000007|X7|SCUI|has_ingredient|R16||RXNORM|RXNORM|||N||",
    # has_tradename in the opposite column order: must not be followed (it would add
    # SCD -> SBD, a wrong-way edge that makes the SCD's chain end at the SBD's BN).
    "C9000004|X4|SCUI|RN|C9000003|X3|SCUI|has_tradename|R17||RXNORM|RXNORM|||N||",
]

MRRANK = "0400|RXNORM|IN|N|\n0500|SNOMEDCT_US|PT|N|\n0450|ICD10CM|PT|N|\n0440|ICD10CM|HT|N|\n"


@pytest.fixture
def meta(tmp_path: Path) -> Path:
    d = tmp_path / "2026AA" / "META"
    d.mkdir(parents=True)
    (d / "MRCONSO.RRF").write_text("\n".join(MRCONSO) + "\n", encoding="utf-8")
    (d / "MRREL.RRF").write_text("\n".join(MRREL) + "\n", encoding="utf-8")
    (d / "MRRANK.RRF").write_text(MRRANK, encoding="utf-8")
    return d


@pytest.fixture
def build(meta: Path, tmp_path: Path) -> Callable[..., Path]:
    def _build(name: str = "t.sqlite", **kw: object) -> Path:
        return build_terminology(
            meta / "MRCONSO.RRF",
            meta / "MRRANK.RRF",
            tmp_path / name,
            mrrel=meta / "MRREL.RRF",
            **kw,  # type: ignore[arg-type]
        )

    return _build


@pytest.fixture
def store(build: Callable[..., Path]) -> TerminologyStore:
    return TerminologyStore(build())


def test_icd10cm_full_chain_dag_and_code_level_walk(store: TerminologyStore) -> None:
    # Both direct parents at distance 1 (the DAG case), T01 only through T01.1 at 2, and not the
    # sibling leaf T01.19, although it shares T01.1's CUI.
    assert store.ancestors("ICD10CM", "T01.10") == [("T01.1", 1), ("T02", 1), ("T01", 2)]
    assert store.ancestors("ICD10CM", "T01.19") == [("T01.1", 1), ("T01", 2)]
    assert store.ancestors("ICD10CM", "T01") == []  # top level: nothing further, not an error


def test_snomed_polyhierarchy_and_one_cui_two_chains(store: TerminologyStore) -> None:
    assert store.ancestors("SNOMEDCT_US", "300") == [("200", 1), ("400", 1), ("100", 2)]
    # 500 is the parent of the CUI's *other* code only: a CUI-level walk mixed the two chains.
    assert store.ancestors("SNOMEDCT_US", "350") == [("500", 1)]
    # The suppressed edge B5 -> B1 is not followed.
    assert store.ancestors("SNOMEDCT_US", "100") == []


def test_descendants_mirror_ancestors(store: TerminologyStore) -> None:
    assert store.descendants("ICD10CM", "T01") == [("T01.1", 1), ("T01.10", 2), ("T01.19", 2)]
    assert store.descendants("ICD10CM", "T01", max_distance=1) == [("T01.1", 1)]
    assert store.descendants("SNOMEDCT_US", "400") == [("300", 1)]
    assert store.descendants("ICD10CM", "T01.10") == []  # a leaf


def test_max_depth_bounds_the_walk(build: Callable[..., Path]) -> None:
    with TerminologyStore(build(hierarchy_max_depth=1)) as s:
        assert s.ancestors("SNOMEDCT_US", "300") == [("200", 1), ("400", 1)]
        assert s.build_info()["hierarchy_max_depth"] == 1
    with pytest.raises(ValueError, match="at least 1"):
        build("zero.sqlite", hierarchy_max_depth=0)


def test_generic_and_branded_products_resolve_to_the_ingredient(store: TerminologyStore) -> None:
    (scd,) = store.ingredients("C9000003")
    assert (scd.cui, scd.rxcui, scd.name, scd.hops) == ("C9000001", "29999", "testdrug", 2)
    # The branded product: two hops by the SBD -> SCDC shortcut, and never the BN node, even
    # though a has_ingredient edge points at it.
    assert [(i.cui, i.hops) for i in store.ingredients("C9000004")] == [("C9000001", 2)]
    # An ingredient resolves to itself.
    assert [(i.cui, i.hops) for i in store.ingredients("C9000001")] == [("C9000001", 0)]


def test_unresolvable_products_are_unresolved_not_mapped(store: TerminologyStore) -> None:
    assert store.ingredients("C9000006") == []  # dose form with no relations
    assert store.ingredients("C9000007") == []  # SBD whose only edge ends at a dead-end BN


def test_mrrel_reader_directions_and_filters(meta: Path) -> None:
    rel = read_relations(meta / "MRREL.RRF", ["ICD10CM", "SNOMEDCT_US"], rxnorm=True)
    # CHD: child AUI -> parent AUIs; the obsolete edge is dropped.
    assert rel.chd["ICD10CM"]["A3"] == {"A2", "A4"}
    assert "B1" not in rel.chd["SNOMEDCT_US"]
    # RxNorm: coarser -> finer, and the SCD gets no edge from the has_tradename row.
    assert rel.rxnorm["C9000004"] == {"C9000003", "C9000002", "C9000005"}
    assert rel.rxnorm["C9000003"] == {"C9000002"}
    with_suppressed = read_relations(
        meta / "MRREL.RRF", ["SNOMEDCT_US"], rxnorm=False, include_suppressed=True
    )
    assert with_suppressed.chd["SNOMEDCT_US"]["B1"] == {"B5"} and not with_suppressed.rxnorm


def test_resolver_depth() -> None:
    graph = {"SBD": {"SCD"}, "SCD": {"SCDC"}, "SCDC": {"IN"}}
    assert list(resolve_ingredients(graph, {"IN"}, ["SCD", "SBD"])) == [
        ("SBD", "IN", 3),
        ("SCD", "IN", 2),
    ]


def test_code_ancestors_rejects_zero_depth() -> None:
    with pytest.raises(ValueError):
        list(code_ancestors({}, {}, max_depth=0))


def test_build_info_and_codes_enrichment(store: TerminologyStore) -> None:
    info = store.build_info()
    assert info["hierarchy_sources"] == ["ICD10CM", "SNOMEDCT_US"]
    assert info["rxnorm_ingredients"] is True and info["hierarchy_max_depth"] is None

    (icd,) = store.codes_for("D9000003", ["ICD10CM"], ancestor_depth=1)
    assert icd.ancestors == ("T01.1", "T02")
    (plain,) = store.codes_for("D9000003", ["ICD10CM"])
    assert plain.ancestors == ()  # not requested: empty means "not looked up"
    (rx,) = store.codes_for("C9000004", ["RXNORM"], ingredients=True)
    assert rx.ingredients == ("29999",)


def test_store_without_mrrel_refuses_hierarchy_requests(
    rrf_dir: Path, tmp_path: Path, terminology_db: Path
) -> None:
    with TerminologyStore(terminology_db) as s:
        assert s.hierarchy_sources == frozenset() and s.has_ingredients is False
        with pytest.raises(ValueError, match="--mrrel"):
            s.require(hierarchy=True)
    config = ExtractionConfig(
        code_systems=("SNOMEDCT_US",), ancestor_depth=2, terminology_db=terminology_db
    )
    with pytest.raises(ValueError, match="no code hierarchy"):
        Extractor(config, engine=FakeEngine())


def test_config_validation() -> None:
    with pytest.raises(ValueError, match="need code_systems"):
        ExtractionConfig(include_ingredients=True)
    with pytest.raises(ValueError, match=">= 0"):
        ExtractionConfig(code_systems=("ICD10CM",), ancestor_depth=-1)


def test_extractor_attaches_ancestors_and_ingredients(meta: Path, tmp_path: Path) -> None:
    # The fake engine's lexicon uses real-looking CUIs; point a store at them by building one
    # whose rows carry those CUIs.
    rows = [
        "C0008031|ENG|P|L1|PF|S1|Y|Q1|||R07.9|ICD10CM|PT|R07.9|Chest pain, unspecified|4|N||",
        "C9|ENG|P|L1|PF|S1|Y|Q2|||R07|ICD10CM|HT|R07|Pain in throat and chest|4|N||",
        "C0025598|ENG|P|L1|PF|S1|Y|Q3|||6809|RXNORM|IN|6809|metformin|0|N||",
    ]
    (meta / "MRCONSO.RRF").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (meta / "MRREL.RRF").write_text(
        "C9|Q2|SDUI|CHD|C0008031|Q1|SDUI||R1||ICD10CM|ICD10CM|||N||\n", encoding="utf-8"
    )
    db = build_terminology(
        meta / "MRCONSO.RRF", meta / "MRRANK.RRF", tmp_path / "x.sqlite", mrrel=meta / "MRREL.RRF"
    )
    config = ExtractionConfig(
        code_systems=("ICD10CM", "RXNORM"),
        ancestor_depth=5,
        include_ingredients=True,
        terminology_db=db,
    )
    with Extractor(config, engine=FakeEngine()) as ex:
        result = ex.process("Chest pain; metformin.", "d")
    codes = {m.cui: m.codes for m in result.mentions}
    assert codes["C0008031"][0].ancestors == ("R07",)
    assert codes["C0025598"][0].ingredients == ("6809",)
