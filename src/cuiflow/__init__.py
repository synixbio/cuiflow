"""cuiflow: clinical concept extraction and UMLS terminology mapping.

Wraps two engines, mmlite (MetaMapLite) and umlsmatch (cTAKES-style), behind one data model,
and maps the concepts they find to SNOMED CT, RxNorm, ICD-10-CM and LOINC codes. See
DESIGN_PLAN.md for the design.

    from cuiflow import extract, load_config

    for result in extract(["No chest pain."], load_config({"mode": "single:umlsmatch"})):
        for m in result.mentions:
            print(m.cui, m.text, m.assertions.negated)
"""

from __future__ import annotations

__version__ = "0.1.0"

from cuiflow.api import DocumentInput, Extractor, extract
from cuiflow.core.config import ExtractionConfig, load_config
from cuiflow.core.models import (
    SCHEMA_VERSION,
    AssertionStatus,
    DocumentResult,
    Mention,
    ProvenanceInfo,
    SectionSpan,
    TerminologyCode,
)

__all__ = [
    "SCHEMA_VERSION",
    "AssertionStatus",
    "DocumentInput",
    "DocumentResult",
    "ExtractionConfig",
    "Extractor",
    "Mention",
    "ProvenanceInfo",
    "SectionSpan",
    "TerminologyCode",
    "__version__",
    "extract",
    "load_config",
]
