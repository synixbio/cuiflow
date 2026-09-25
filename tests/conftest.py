"""Shared fixtures. Unit tests use a fake engine and synthetic RRF rows: no UMLS data, no spaCy.

Everything here is invented text. Never put real notes in a fixture.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from cuiflow.core.models import AssertionStatus, Mention, ProvenanceInfo, make_mention_id
from cuiflow.engines.base import EngineInfo

# phrase -> (cui, preferred_name, group)
LEXICON = {
    "chest pain": ("C0008031", "Chest Pain", "FINDING"),
    "pain": ("C0030193", "Pain", "FINDING"),
    "diabetes": ("C0011849", "Diabetes Mellitus", "DISORDER"),
    "metformin": ("C0025598", "metformin", "DRUG"),
}


class FakeEngine:
    """Finds LEXICON phrases; negates a mention when its sentence has 'no ' before it."""

    def __init__(self, name: str = "fake", *, fail_on: str | None = None) -> None:
        self.name = name
        self.fail_on = fail_on
        self.closed = False

    def info(self) -> EngineInfo:
        return EngineInfo(name=self.name, version="0")

    def extract(self, text: str, doc_id: str) -> list[Mention]:
        if self.fail_on is not None and self.fail_on in text:
            raise RuntimeError("boom " + text)  # message quotes the text, as real errors can
        out = []
        lower = text.lower()
        for phrase, (cui, name, group) in LEXICON.items():
            start = lower.find(phrase)
            if start < 0:
                continue
            end = start + len(phrase)
            out.append(
                Mention(
                    mention_id=make_mention_id(doc_id, start, end, cui),
                    start=start,
                    end=end,
                    text=text[start:end],
                    cui=cui,
                    preferred_name=name,
                    semantic_group=group,
                    assertions=AssertionStatus(
                        negated="no " in lower[lower.rfind(".", 0, start) + 1 : start]
                    ),
                    provenance=ProvenanceInfo(found_by=(self.name,)),
                )
            )
        return sorted(out, key=lambda m: (m.start, m.end))

    def close(self) -> None:
        self.closed = True


class FakeAssessor(FakeEngine):
    """Assesses another engine's mentions: history_of=True for all; None past offset 100."""

    supports_assess = True

    def assess(self, text: str, mentions: list[Mention]) -> list[AssertionStatus | None]:
        return [
            None if m.start >= 100 else AssertionStatus(negated=False, history_of=True)
            for m in mentions
        ]


@pytest.fixture
def fake_engine() -> FakeEngine:
    return FakeEngine()


def _mrconso(
    cui: str,
    sab: str,
    tty: str,
    code: str,
    s: str,
    *,
    aui: str,
    lat: str = "ENG",
    suppress: str = "N",
) -> str:
    # CUI|LAT|TS|LUI|STT|SUI|ISPREF|AUI|SAUI|SCUI|SDUI|SAB|TTY|CODE|STR|SRL|SUPPRESS|CVF
    return (
        "|".join(
            [
                cui,
                lat,
                "P",
                "L1",
                "PF",
                "S1",
                "Y",
                aui,
                "",
                "",
                "",
                sab,
                tty,
                code,
                s,
                "0",
                suppress,
                "",
            ]
        )
        + "|\n"
    )


@pytest.fixture
def rrf_dir(tmp_path: Path) -> Path:
    """A tiny synthetic META directory, named like a real release."""
    meta = tmp_path / "2026AA" / "META"
    meta.mkdir(parents=True)
    rows = [
        # Chest pain: SNOMED PT + SY, ICD10CM PT.
        _mrconso("C0008031", "SNOMEDCT_US", "PT", "29857009", "Chest pain", aui="A1"),
        _mrconso("C0008031", "SNOMEDCT_US", "SY", "29857009", "Pain in chest", aui="A2"),
        _mrconso("C0008031", "ICD10CM", "PT", "R07.9", "Chest pain, unspecified", aui="A3"),
        # Metformin: RxNorm has no PT; MRRANK decides (IN outranks SY).
        _mrconso("C0025598", "RXNORM", "SY", "6809", "metformine", aui="A4"),
        _mrconso("C0025598", "RXNORM", "IN", "6809", "metformin", aui="A5"),
        # Excluded: suppressed, non-English, unwanted source.
        _mrconso("C0011849", "SNOMEDCT_US", "PT", "73211009", "Old name", aui="A6", suppress="O"),
        _mrconso("C0011849", "SNOMEDCT_US", "PT", "73211009", "Diabète", aui="A7", lat="FRE"),
        _mrconso("C0011849", "MSH", "MH", "D003920", "Diabetes Mellitus", aui="A8"),
        _mrconso("C0011849", "SNOMEDCT_US", "PT", "73211009", "Diabetes mellitus", aui="A9"),
    ]
    (meta / "MRCONSO.RRF").write_text("".join(rows), encoding="utf-8")
    # RANK|SAB|TTY|SUPPRESS — higher rank is more preferred.
    (meta / "MRRANK.RRF").write_text(
        "0400|RXNORM|IN|N|\n0300|RXNORM|SY|N|\n0500|SNOMEDCT_US|PT|N|\n0490|SNOMEDCT_US|SY|N|\n"
        "0450|ICD10CM|PT|N|\n",
        encoding="utf-8",
    )
    return meta


@pytest.fixture
def terminology_db(rrf_dir: Path, tmp_path: Path) -> Path:
    from cuiflow.terminology.builder import build_terminology

    return build_terminology(
        rrf_dir / "MRCONSO.RRF", rrf_dir / "MRRANK.RRF", tmp_path / "terminology.sqlite"
    )


@pytest.fixture
def patch_extractor(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Make every Extractor built by the interfaces use a FakeEngine."""
    import cuiflow.api

    real = cuiflow.api.Extractor

    def install(**engine_kwargs: object) -> None:
        def factory(config=None, **kw):  # type: ignore[no-untyped-def]
            kw.setdefault("engine", FakeEngine(**engine_kwargs))  # type: ignore[arg-type]
            return real(config, **kw)

        monkeypatch.setattr(cuiflow.api, "Extractor", factory)
        import cuiflow.interfaces.batch as batch

        monkeypatch.setattr(batch, "Extractor", factory)

    return install
