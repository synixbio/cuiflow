"""The REST service (DESIGN_PLAN §5, Phase 5). Needs the ``server`` extra.

    cuiflow serve --port 8000          # or: uvicorn --factory cuiflow.interfaces.rest:create_app

**Every request body is PHI.** Nothing here logs request content; run it with access logs
treated as PHI and, outside localhost, behind TLS. Set ``CUIFLOW_API_TOKEN`` to require
``Authorization: Bearer <token>`` on every endpoint except the probes, ``/health`` and
``/ready``. ``cuiflow serve`` refuses a non-loopback host without a token.

A failed extraction returns 500 naming only the exception type; the exception is not logged,
because parser errors often quote the input. When every extractor is busy, ``/extract`` returns
503 with ``Retry-After``. ``/concepts/{cui}`` does not wait for an extractor: it reads its own
connection to the terminology database, so a code lookup is served under extraction load.

``/metrics`` serves Prometheus metrics (behind the same token) when ``prometheus-client`` is
installed, as the ``server`` extra does. No metric label carries request content
(:mod:`cuiflow.interfaces.metrics`).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from cuiflow import __version__
from cuiflow.api import Extractor
from cuiflow.core.config import ExtractionConfig, load_config
from cuiflow.interfaces import (
    MAX_ANCESTOR_DEPTH,
    MAX_CHARS,
    MAX_DOC_ID_CHARS,
    TOKEN_ENV,
    bearer_matches,
)
from cuiflow.interfaces.pool import ExtractorPool, PoolExhausted
from cuiflow.terminology.store import TerminologyStore

if TYPE_CHECKING:
    from cuiflow.interfaces.metrics import ServiceMetrics


class ExtractRequest(BaseModel):
    """One document. The body is PHI."""

    text: str = Field(max_length=MAX_CHARS)
    doc_id: str = Field(default="doc", max_length=MAX_DOC_ID_CHARS)


def create_app(config: ExtractionConfig | None = None, pool_size: int = 1) -> FastAPI:
    cfg = config or load_config()
    token = os.environ.get(TOKEN_ENV) or None
    state: dict[str, ExtractorPool] = {}
    # Concept lookups: one read-only store, apart from the extractors, behind a lock.
    lookups: dict[str, TerminologyStore] = {}
    lookup_lock = threading.Lock()
    metrics = _metrics(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state["pool"] = ExtractorPool(lambda: Extractor(cfg), size=pool_size)
        if cfg.terminology_db is not None:
            lookups["store"] = TerminologyStore(cfg.terminology_db)
        if metrics is not None:
            metrics.watch_pool(state["pool"])
        try:
            yield
        finally:
            if metrics is not None:
                metrics.watch_pool(None)
            state.pop("pool").close()
            if "store" in lookups:
                lookups.pop("store").close()

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if token is None:
            return
        if not bearer_matches(authorization, token):
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    app = FastAPI(
        title="cuiflow",
        version=__version__,
        description=(
            "Clinical concept extraction and UMLS terminology mapping. Request bodies contain "
            "PHI: run egress-blocked and treat access logs as PHI."
        ),
        lifespan=lifespan,
    )
    auth = [Depends(require_token)]

    @app.exception_handler(PoolExhausted)
    async def busy(request: Request, exc: PoolExhausted) -> Response:
        if metrics is not None:
            metrics.pool_exhausted.inc()
        return JSONResponse(
            {"detail": "busy; retry later"}, status_code=503, headers={"Retry-After": "1"}
        )

    if metrics is not None:
        m = metrics

        @app.middleware("http")
        async def count_requests(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            started = time.perf_counter()
            status = 500
            try:
                response = await call_next(request)
                status = response.status_code
                return response
            finally:
                route = getattr(request.scope.get("route"), "path", "unmatched")
                m.requests.labels(route, request.method, str(status)).inc()
                m.request_seconds.labels(route).observe(time.perf_counter() - started)

        @app.get("/metrics", dependencies=auth, include_in_schema=False)
        def prometheus() -> Response:
            from cuiflow.interfaces.metrics import CONTENT_TYPE_LATEST

            return Response(m.render(), media_type=CONTENT_TYPE_LATEST)

    # async: served on the event loop, so probes still answer when every worker thread is
    # busy running a long /extract.
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> Response:
        # Unauthenticated like /health, for probes; they read the status code.
        up = "pool" in state
        return JSONResponse({"ready": up}, status_code=200 if up else 503)

    @app.get("/info", dependencies=auth)
    async def info() -> dict[str, Any]:
        return {"cuiflow": __version__, "config": cfg.to_dict(), **state["pool"].info}

    @app.post("/extract", dependencies=auth)
    def extract(req: ExtractRequest) -> dict[str, Any]:
        with state["pool"].lease() as ex:
            started = time.perf_counter()
            try:
                doc = ex.process(req.text, req.doc_id)
            except Exception as exc:  # the type only: the message may quote the note
                kind = type(exc).__name__
                if metrics is not None:
                    metrics.extract_errors.labels(kind).inc()
                raise HTTPException(status_code=500, detail=f"extraction failed: {kind}") from None
            if metrics is not None:
                metrics.extract_seconds.observe(time.perf_counter() - started)
                metrics.documents.inc()
                metrics.mentions.inc(len(doc.mentions))
                metrics.document_chars.observe(len(req.text))
            result: dict[str, Any] = doc.to_dict()
            return result

    @app.get("/concepts/{cui}", dependencies=auth)
    def concept(
        cui: str,
        ancestor_depth: int = Query(default=0, ge=0, le=MAX_ANCESTOR_DEPTH),
        ingredients: bool = False,
    ) -> dict[str, Any]:
        """Codes for a CUI. ``ancestor_depth`` > 0 adds ICD-10-CM / SNOMED CT ancestors;
        ``ingredients`` adds RxNorm ingredients (both need a store built with MRREL)."""
        store = lookups.get("store")
        if store is None:
            raise HTTPException(status_code=404, detail="no terminology database configured")
        try:
            store.require(hierarchy=ancestor_depth > 0, ingredients=ingredients)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        with lookup_lock:
            codes = store.codes_for(cui, ancestor_depth=ancestor_depth, ingredients=ingredients)
            display = store.preferred_display(cui)
        return {"cui": cui, "display": display, "codes": [c.to_dict() for c in codes]}

    return app


def _metrics(cfg: ExtractionConfig) -> ServiceMetrics | None:
    try:
        from cuiflow.interfaces.metrics import ServiceMetrics
    except ImportError:  # prometheus-client not installed: serve without /metrics
        return None
    return ServiceMetrics(version=__version__, mode=cfg.mode.value, profile=cfg.profile)
