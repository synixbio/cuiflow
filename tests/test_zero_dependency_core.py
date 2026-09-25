"""The core must import with the standard library alone (DESIGN_PLAN §2.2)."""

from __future__ import annotations

import subprocess
import sys

CORE_MODULES = [
    "cuiflow",
    "cuiflow.core.models",
    "cuiflow.core.config",
    "cuiflow.core.manifests",
    "cuiflow.core.alignment",
    "cuiflow.terminology.store",
    "cuiflow.terminology.builder",
    "cuiflow.io.readers",
    "cuiflow.io.writers.jsonl",
    "cuiflow.io.writers.omop",
    "cuiflow.evaluation",
    "cuiflow.interfaces.cli",
    "cuiflow.interfaces.batch",
]
THIRD_PARTY = ["mmlite", "umlsmatch", "spacy", "fastapi", "pydantic", "mcp", "pyarrow"]


def test_core_imports_no_third_party_package() -> None:
    code = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in CORE_MODULES)
        + f"loaded = [p for p in {THIRD_PARTY!r} if p in sys.modules]\n"
        + "print(','.join(loaded))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
