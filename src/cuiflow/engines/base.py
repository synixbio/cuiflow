"""The interface every engine adapter implements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from cuiflow.core.models import Mention


@dataclass(frozen=True, slots=True)
class EngineInfo:
    """What a run manifest records about an engine."""

    name: str
    version: str
    dictionary: str | None = None  # path of the index or dictionary it loaded
    umls_release: str | None = None  # when the index records it
    settings: dict[str, Any] = field(default_factory=dict, hash=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "dictionary": self.dictionary,
            "umls_release": self.umls_release,
            "settings": dict(self.settings),
        }


@runtime_checkable
class Engine(Protocol):
    """A concept extractor producing canonical mentions.

    Engines are **not** thread-safe (neither mmlite nor umlsmatch is): use one per thread or
    process, or lease them from a pool. Codes are not attached here; the Extractor does that.
    """

    name: str

    def info(self) -> EngineInfo: ...

    def extract(self, text: str, doc_id: str) -> list[Mention]: ...

    def close(self) -> None: ...
