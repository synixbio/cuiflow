from __future__ import annotations

from pathlib import Path

import pytest

from cuiflow.terminology.builder import build_terminology, guess_release
from cuiflow.terminology.store import TerminologyStore


def test_preferred_code_rules(terminology_db: Path) -> None:
    with TerminologyStore(terminology_db) as store:
        snomed = store.codes_for("C0008031", ["SNOMEDCT_US"])
        assert {(c.code, c.tty, c.is_preferred) for c in snomed} == {
            ("29857009", "PT", True),
            ("29857009", "SY", False),
        }
        # No PT in RxNorm: MRRANK's best term type (IN) becomes preferred.
        rx = store.codes_for("C0025598", ["RXNORM"], preferred_only=True)
        assert [(c.code, c.tty) for c in rx] == [("6809", "IN")]


def test_icd10cm_is_marked_crosswalk(terminology_db: Path) -> None:
    with TerminologyStore(terminology_db) as store:
        (icd,) = store.codes_for("C0008031", ["ICD10CM"])
        assert icd.is_crosswalk and icd.code == "R07.9"
        assert store.cuis_for_code("ICD10CM", "R07.9") == ["C0008031"]


def test_suppressed_foreign_and_unwanted_rows_excluded(terminology_db: Path) -> None:
    with TerminologyStore(terminology_db) as store:
        rows = store.codes_for("C0011849")
        assert [(c.system, c.display) for c in rows] == [("SNOMEDCT_US", "Diabetes mellitus")]


def test_build_info_records_release(terminology_db: Path) -> None:
    with TerminologyStore(terminology_db) as store:
        info = store.build_info()
    assert info["umls_release"] == "2026AA"
    assert info["include_suppressed"] is False


def test_refuses_to_overwrite(rrf_dir: Path, terminology_db: Path) -> None:
    with pytest.raises(FileExistsError):
        build_terminology(rrf_dir / "MRCONSO.RRF", rrf_dir / "MRRANK.RRF", terminology_db)


def test_guess_release(tmp_path: Path) -> None:
    assert guess_release(tmp_path / "umls-2025AB" / "META" / "MRCONSO.RRF") == "2025AB"
    assert guess_release(tmp_path / "META" / "MRCONSO.RRF") is None


def test_missing_store_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="build-terminology"):
        TerminologyStore(tmp_path / "nope.sqlite")


def test_preferred_display_ranks_sources_not_alphabetically(terminology_db: Path) -> None:
    with TerminologyStore(terminology_db) as store:
        # "ICD10CM" sorts before "SNOMEDCT_US", but its crosswalk wording should not win.
        assert store.preferred_display("C0008031") == "Chest pain"
        assert store.preferred_display("C9999999") is None
