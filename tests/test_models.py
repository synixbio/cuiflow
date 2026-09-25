from __future__ import annotations

import json

import pytest

from cuiflow.core.models import (
    AssertionStatus,
    DocumentResult,
    Mention,
    ProvenanceInfo,
    TerminologyCode,
    make_mention_id,
)


def _mention(**kw: object) -> Mention:
    base: dict[str, object] = {
        "mention_id": make_mention_id("d", 0, 4, "C1"),
        "start": 0,
        "end": 4,
        "text": "pain",
        "cui": "C1",
        "preferred_name": "Pain",
        "semantic_group": "FINDING",
        "assertions": AssertionStatus(negated=True),
    }
    base.update(kw)
    return Mention(**base)  # type: ignore[arg-type]


def test_round_trip_preserves_none_versus_false() -> None:
    m = _mention(
        assertions=AssertionStatus(negated=False, history_of=None, subject="patient"),
        codes=(TerminologyCode("ICD10CM", "R52", "PT", "Pain", True, is_crosswalk=True),),
        provenance=ProvenanceInfo(
            found_by=("mmlite", "umlsmatch"),
            engine_assertions={"mmlite": {"negated": False}},
            assertion_conflict=True,
            assessed_by="umlsmatch",
        ),
    )
    doc = DocumentResult(doc_id="d", mentions=(m,), metadata={"mode": "x"})
    back = DocumentResult.from_dict(json.loads(json.dumps(doc.to_dict())))
    assert back == doc
    a = back.mentions[0].assertions
    assert a.negated is False
    assert a.history_of is None  # not assessed stays None, never becomes False
    assert a.assessed() == {"negated", "subject"}


def test_mention_id_does_not_embed_doc_id() -> None:
    mid = make_mention_id("patient-12345/note.txt", 3, 9, "C0008031")
    assert "12345" not in mid and len(mid) == 16
    assert mid == make_mention_id("patient-12345/note.txt", 3, 9, "C0008031")


def test_text_is_omitted_unless_included() -> None:
    assert "text" not in DocumentResult(doc_id="d", mentions=()).to_dict()
    assert DocumentResult(doc_id="d", mentions=(), text="note").to_dict()["text"] == "note"


def test_invalid_span_rejected() -> None:
    with pytest.raises(ValueError):
        _mention(start=5, end=2)


def test_metadata_is_read_only_and_results_hashable() -> None:
    doc = DocumentResult(doc_id="d", mentions=(_mention(),), metadata={"a": 1})
    with pytest.raises(TypeError):
        doc.metadata["a"] = 2  # type: ignore[index]
    hash(doc.mentions[0])


def test_unknown_schema_version_rejected() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        DocumentResult.from_dict({"schema_version": "99", "doc_id": "d", "mentions": []})


def test_peak_rss_is_reported() -> None:
    # It silently returned None on Windows until the ctypes signatures were declared.
    from cuiflow.core.manifests import peak_rss_mb

    peak = peak_rss_mb()
    assert peak is not None and peak > 0


@pytest.mark.parametrize(
    ("experiencer", "subject"),
    [("Patient", "patient"), ("Other", "other"), ("Family", "family_member"), ("", None)],
)
def test_mmlite_experiencer_mapping(experiencer: str, subject: str | None) -> None:
    from types import SimpleNamespace

    from cuiflow.engines.mmlite import MmliteEngine

    engine = MmliteEngine.__new__(MmliteEngine)  # the mapping needs no mmlite index
    engine._negation = "context"
    entity = SimpleNamespace(negated=False, experiencer=experiencer, temporality="Historical")
    status = engine._assertions(entity)
    # "family history of X" reaches cuiflow as experiencer "Other" (mmlite's ConText has no
    # family value), so it is subject "other", not "family_member".
    assert status.subject == subject and status.temporality == "historical"
