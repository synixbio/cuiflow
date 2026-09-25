"""Prometheus metrics for the REST service (DESIGN_PLAN §11, Phase 5). Needs ``prometheus-client``.

**No label carries request content.** Endpoints are labelled by their route template
(``/concepts/{cui}``, never the CUI asked for), and document sizes and mention counts are
histograms and counters, never per-document series. Nothing here can hold note text, a document
key or a code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

if TYPE_CHECKING:
    from cuiflow.interfaces.pool import ExtractorPool

__all__ = ["CONTENT_TYPE_LATEST", "ServiceMetrics"]

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.35, 0.5, 1, 2, 5, 10, 30)
_CHAR_BUCKETS = (500, 1_000, 2_500, 5_000, 10_000, 25_000, 50_000, 100_000, 200_000)


class ServiceMetrics:
    """One registry per app, so several apps (tests) never collide in the global registry."""

    def __init__(self, *, version: str, mode: str, profile: str) -> None:
        self.registry = CollectorRegistry()
        r = self.registry
        Gauge(
            "cuiflow_info",
            "Service version and configuration (always 1).",
            ["version", "mode", "profile"],
            registry=r,
        ).labels(version, mode, profile).set(1)
        self.requests = Counter(
            "cuiflow_http_requests",
            "HTTP requests by route template and status code.",
            ["route", "method", "status"],
            registry=r,
        )
        self.request_seconds = Histogram(
            "cuiflow_http_request_seconds",
            "HTTP request latency by route template.",
            ["route"],
            buckets=_LATENCY_BUCKETS,
            registry=r,
        )
        self.documents = Counter("cuiflow_documents", "Documents extracted.", registry=r)
        self.mentions = Counter("cuiflow_mentions", "Mentions returned.", registry=r)
        self.extract_errors = Counter(
            "cuiflow_extract_errors",
            "Extractions that raised, by exception type.",
            ["error_type"],
            registry=r,
        )
        self.extract_seconds = Histogram(
            "cuiflow_extract_seconds",
            "Time inside the extractor per document (excludes waiting for a lease).",
            buckets=_LATENCY_BUCKETS,
            registry=r,
        )
        self.document_chars = Histogram(
            "cuiflow_document_chars",
            "Document length in characters.",
            buckets=_CHAR_BUCKETS,
            registry=r,
        )
        self.pool_exhausted = Counter(
            "cuiflow_pool_exhausted",
            "Requests refused with 503 because no extractor was free in time.",
            registry=r,
        )
        self._pool_size = Gauge("cuiflow_pool_size", "Extractors in the pool.", registry=r)
        self._pool_in_use = Gauge("cuiflow_pool_in_use", "Extractors leased right now.", registry=r)

    def watch_pool(self, pool: ExtractorPool | None) -> None:
        """Report the pool's size and use on every scrape (0 while starting or stopped)."""
        self._pool_size.set_function(lambda: pool.size if pool is not None else 0)
        self._pool_in_use.set_function(lambda: pool.in_use if pool is not None else 0)

    def render(self) -> bytes:
        return generate_latest(self.registry)
