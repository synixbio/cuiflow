"""The one entry point every interface uses (DESIGN_PLAN §6.1).

The CLI, batch runner, REST service and MCP server are thin wrappers around :class:`Extractor`
and add no extraction logic of their own.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from cuiflow.core.alignment import longest_non_overlapping
from cuiflow.core.config import ExtractionConfig, load_config
from cuiflow.core.enums import EngineMode, OverlapPolicy
from cuiflow.core.filters import guideline_filter, unassess_heading_history
from cuiflow.core.manifests import sha256_text
from cuiflow.core.models import DocumentResult, Mention
from cuiflow.engines import Engine, create_engine
from cuiflow.terminology.store import TerminologyStore


@dataclass(frozen=True, slots=True)
class DocumentInput:
    doc_id: str
    text: str
    #: Byte sequences in the source file that were not valid UTF-8 and became U+FFFD. The text,
    #: and so ``content_sha256``, differ from the file's bytes when this is not 0.
    replaced: int = 0


class Extractor:
    """Engines plus terminology store, loaded once and reused across documents.

    Not thread-safe (neither engine is). Use one per thread or process, or a pool.

    Without ``config``, settings come from :func:`~cuiflow.core.config.load_config`: the
    ``CUIFLOW_*`` environment variables and ``./cuiflow.toml`` if the working directory has one,
    so the same code can behave differently when run from another directory. Pass an
    :class:`~cuiflow.core.config.ExtractionConfig` to use exactly the settings given.
    """

    def __init__(
        self,
        config: ExtractionConfig | None = None,
        *,
        engine: Engine | None = None,
        store: TerminologyStore | None = None,
    ) -> None:
        self.config = config or load_config()
        if self.config.code_systems and store is None and self.config.terminology_db is None:
            raise ValueError(
                "code_systems needs a terminology database: set terminology_db "
                "(build one with: cuiflow build-terminology)"
            )
        self.store = store
        # Only what this object opened is closed by close(); injected resources are the caller's.
        self._owns_store = store is None and self.config.terminology_db is not None
        self._owns_engine = engine is None
        if store is None and self.config.terminology_db is not None:
            self.store = TerminologyStore(self.config.terminology_db)
        try:
            if self.store is not None:
                self.store.require(
                    hierarchy=self.config.ancestor_depth > 0,
                    ingredients=self.config.include_ingredients,
                )
            self.engine = engine if engine is not None else create_engine(self.config)
        except BaseException:
            # A store opened here would otherwise hold its file (locked, on Windows) until GC.
            if self._owns_store and self.store is not None:
                self.store.close()
            raise

    def info(self) -> dict[str, Any]:
        return {
            "engine": self.engine.info().to_dict(),
            "terminology": self.store.build_info() if self.store else None,
        }

    def _has_standard_code(self, cui: str) -> bool:
        if self.store is None:
            return True  # nothing to prefer by: keep the engine's order
        return bool(self.store.codes_for(cui, ("SNOMEDCT_US", "RXNORM")))

    def _postprocess(self, text: str, mentions: list[Mention]) -> list[Mention]:
        cfg = self.config
        # umlsmatch applies `longest` itself; mmlite and the ensembles need it here.
        if cfg.overlaps is OverlapPolicy.LONGEST and cfg.mode is not EngineMode.UMLSMATCH:
            mentions = longest_non_overlapping(mentions)
        mentions = unassess_heading_history(text, mentions)
        # Groups first: the guidelines' one-concept-per-span choice is then made among the
        # concepts the caller asked for, and never empties a span of an in-scope concept.
        if cfg.semantic_groups:
            wanted = {g.upper() for g in cfg.semantic_groups}
            mentions = [m for m in mentions if m.semantic_group.upper() in wanted]
        if cfg.mention_filter == "guidelines":
            mentions = guideline_filter(text, mentions, has_standard_code=self._has_standard_code)
        if cfg.code_systems and self.store is not None:
            store = self.store
            mentions = [
                dataclasses.replace(
                    m,
                    codes=store.codes_for(
                        m.cui,
                        cfg.code_systems,
                        ancestor_depth=cfg.ancestor_depth,
                        ingredients=cfg.include_ingredients,
                    ),
                )
                for m in mentions
            ]
        return mentions

    def process(self, text: str, doc_id: str = "doc") -> DocumentResult:
        started = time.perf_counter()
        mentions = self._postprocess(text, self.engine.extract(text, doc_id))
        metadata: dict[str, Any] = {
            "mode": self.config.mode.value,
            "profile": self.config.profile,
            "content_sha256": sha256_text(text),
        }
        if self.config.mention_filter == "guidelines":
            # Without a store the filter cannot prefer standard-coded CUIs, so the same note
            # can keep different concepts; say which rule chose them.
            metadata["guideline_tie_break"] = (
                "standard_code" if self.store is not None else "engine_order"
            )
        metadata["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return DocumentResult(
            doc_id=doc_id,
            mentions=tuple(mentions),
            text=text if self.config.include_text else None,
            metadata=metadata,
        )

    def run(self, documents: Iterable[str | DocumentInput]) -> Iterator[DocumentResult]:
        for n, doc in enumerate(documents, 1):
            if isinstance(doc, DocumentInput):
                yield self.process(doc.text, doc.doc_id)
            else:
                yield self.process(doc, f"doc-{n:06d}")

    def close(self) -> None:
        """Close the engine and store this extractor created; injected ones are left open."""
        try:
            if self._owns_engine:
                self.engine.close()
        finally:
            if self._owns_store and self.store is not None:
                self.store.close()

    def __enter__(self) -> Extractor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def extract(
    documents: Iterable[str | DocumentInput],
    config: ExtractionConfig | None = None,
) -> Iterator[DocumentResult]:
    """Stream documents through extraction and code enrichment.

    Engines load on the first document and close when the iterator is exhausted or closed.
    For repeated calls, keep an :class:`Extractor` instead.
    """
    with Extractor(config) as extractor:
        yield from extractor.run(documents)
