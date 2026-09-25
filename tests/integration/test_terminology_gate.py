"""The Phase 2 gate (DESIGN_PLAN §11): the cases metamap_api's examples were checked against.

Needs a store built from UMLS 2026AA with MRREL::

    cuiflow build-terminology --mrconso META/MRCONSO.RRF --mrrank META/MRRANK.RRF \\
        --mrrel META/MRREL.RRF --out data/terminology.sqlite
    CUIFLOW_TERMINOLOGY_DB=data/terminology.sqlite pytest -m integration

The codes are UMLS 2026AA's; another release may legitimately differ.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from cuiflow.terminology.store import TerminologyStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("CUIFLOW_TERMINOLOGY_DB"), reason="CUIFLOW_TERMINOLOGY_DB not set"
    ),
]


@pytest.fixture(scope="module")
def store() -> Iterator[TerminologyStore]:
    with TerminologyStore(os.environ["CUIFLOW_TERMINOLOGY_DB"]) as s:
        info = s.build_info()
        if info["umls_release"] != "2026AA" or not s.hierarchy_sources or not s.has_ingredients:
            pytest.skip("needs a UMLS 2026AA store built with --mrrel")
        yield s


def test_orthopnea_chain_keeps_r06(store: TerminologyStore) -> None:
    # R06 shares a CUI with the leaf R06.9; a CUI-level walk dropped it from this chain.
    assert store.ancestors("ICD10CM", "R06.01") == [
        ("R06.0", 1),
        ("R06", 2),
        ("R00-R09", 3),
        ("R00-R99", 4),
        ("ICD-10-CM", 5),
    ]
    assert store.cuis_for_code("ICD10CM", "R06") == store.cuis_for_code("ICD10CM", "R06.9")


def test_jaw_preferred_code_from_mrrank_fallback(store: TerminologyStore) -> None:
    # "Jaw" has no SNOMED CT PT row; without the MRRANK fallback it had no preferred code.
    codes = store.codes_for("C0022359", ["SNOMEDCT_US"])
    assert all(c.tty != "PT" for c in codes)
    assert [c.code for c in codes if c.is_preferred] == ["661005"]


def test_lisinopril_ingredient_paths(store: TerminologyStore) -> None:
    lisinopril = ("C0065374", "29046", "lisinopril")
    # SCD: lisinopril 10 MG Oral Tablet, two hops (SCD -> SCDC -> IN).
    (scd_cui,) = store.cuis_for_code("RXNORM", "314076")
    assert [(i.cui, i.rxcui, i.name, i.hops) for i in store.ingredients(scd_cui)] == [
        (*lisinopril, 2)
    ]
    # Branded: Zestril and Prinivil SBDs resolve to the same ingredient, never to the BN node.
    for sbd in ("104377", "206765"):
        (cui,) = store.cuis_for_code("RXNORM", sbd)
        assert [(i.cui, i.rxcui, i.name) for i in store.ingredients(cui)] == [lisinopril]
    # A combination product has both ingredients.
    (combo,) = store.cuis_for_code("RXNORM", "197885")  # HCTZ 12.5 MG / lisinopril 10 MG
    assert {i.rxcui for i in store.ingredients(combo)} == {"5487", "29046"}
    # The ingredient resolves to itself.
    assert [(i.cui, i.hops) for i in store.ingredients("C0065374")] == [("C0065374", 0)]


def test_one_cui_two_snomed_chains_stay_apart(store: TerminologyStore) -> None:
    # C0065374 owns the substance and the product; their parents have nothing in common.
    substance = {a for a, _ in store.ancestors("SNOMEDCT_US", "386873009", max_distance=1)}
    product = {a for a, _ in store.ancestors("SNOMEDCT_US", "108575001", max_distance=1)}
    assert substance and product and not substance & product
