"""A bounded pool of Extractors for the REST service (DESIGN_PLAN §7.2).

Follows umlsmatch's service design: a fixed number of extractors, leased per request. A request
that cannot get one within ``lease_timeout`` fails (the service returns 503) instead of queueing
without limit, so memory is ``size`` times one extractor and nothing more.
"""

from __future__ import annotations

import queue
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any

from cuiflow.api import Extractor


class PoolExhausted(RuntimeError):
    """No extractor became free within the lease timeout."""


class ExtractorPool:
    def __init__(
        self, factory: Callable[[], Extractor], size: int = 1, lease_timeout: float = 30.0
    ) -> None:
        if size < 1:
            raise ValueError("pool size must be >= 1")
        self.size = size
        self.lease_timeout = lease_timeout
        self._free: queue.Queue[Extractor] = queue.Queue()
        self._all: list[Extractor] = []
        #: The extractors' engine and terminology info, read once: it does not change, and
        #: reading it must not need a lease (a busy pool would make /info fail).
        self.info: dict[str, Any] = {}
        try:
            for _ in range(size):
                ex = factory()
                self._all.append(ex)
                self._free.put(ex)
            self.info = self._all[0].info()
        except BaseException:
            with suppress(Exception):  # keep the factory's error, not a cleanup one
                self.close()
            raise

    @property
    def in_use(self) -> int:
        """Extractors leased right now."""
        return len(self._all) - self._free.qsize()

    @contextmanager
    def lease(self) -> Iterator[Extractor]:
        try:
            ex = self._free.get(timeout=self.lease_timeout)
        except queue.Empty:
            raise PoolExhausted(f"no extractor free within {self.lease_timeout}s") from None
        try:
            yield ex
        finally:
            self._free.put(ex)

    def close(self) -> None:
        """Close every extractor, even if one fails; the first failure is re-raised."""
        first: Exception | None = None
        for ex in self._all:
            try:
                ex.close()
            except Exception as exc:
                first = first or exc
        self._all.clear()
        if first is not None:
            raise first
