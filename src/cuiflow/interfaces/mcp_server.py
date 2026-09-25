"""The MCP server (DESIGN_PLAN §9). Needs the ``mcp`` extra (MCP Python SDK 2.x).

    cuiflow mcp                                    # stdio
    cuiflow mcp --transport http --port 8765       # streamable HTTP at /mcp

Tool results go into an agent's context, so they are compact and capped: one entry per
distinct CUI, offsets only on request.

**Limits.** Tools take what the REST service takes: a note of at most ``MAX_CHARS``
characters, known code systems, and counts within stated bounds. A call outside them returns an
``error`` and does no work.

**HTTP.** Set ``CUIFLOW_API_TOKEN`` (the REST service's variable) to require
``Authorization: Bearer <token>`` on every request, websocket or otherwise. The CLI refuses to
listen on a non-loopback address without one. On loopback the SDK's DNS-rebinding protection is on.

**Privacy.** By the time an agent calls ``extract_clinical_concepts``, the note is already in
its conversation and has been sent to its model provider. This server keeps the extraction
local; it does not keep the note local. Nothing here logs, caches or writes note text, and no
tool reads a file path. A tool that fails unexpectedly returns the exception type as its
``error``; the exception itself, whose message may quote the note, is dropped rather than left
for the SDK to log.
"""

from __future__ import annotations

import functools
import logging
import threading
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from cuiflow import __version__
from cuiflow.api import Extractor
from cuiflow.core.config import ExtractionConfig, load_config
from cuiflow.core.enums import CODE_SYSTEMS
from cuiflow.core.models import DocumentResult
from cuiflow.interfaces import MAX_ANCESTOR_DEPTH, MAX_CHARS, bearer_matches

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
DEFAULT_CODE_SYSTEMS = ("SNOMEDCT_US", "RXNORM", "ICD10CM")
_NOT_ASSESSED = "not assessed"
MAX_CONCEPTS = 500
MAX_RESULTS = 100
MAX_QUERY_CHARS = 1_000

_log = logging.getLogger(__name__)
ToolResult = dict[str, Any]


def _contained(tool: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
    """``tool``, returning ``{"error": <exception type>}`` instead of raising. The SDK would log
    an escaped exception with its traceback, and the message can quote the note."""

    @functools.wraps(tool)
    def run(*args: Any, **kwargs: Any) -> ToolResult:
        try:
            return tool(*args, **kwargs)
        except Exception as exc:
            error_type = type(exc).__name__
            _log.warning("tool %s failed: %s", tool.__name__, error_type)
            return {"error": f"internal error ({error_type})"}

    return run


def _out_of_range(name: str, value: int, top: int) -> dict[str, Any] | None:
    if 1 <= value <= top:
        return None
    return {"error": f"{name} must be between 1 and {top}, got {value}"}


#: Common names for the UMLS source abbreviations, offered as hints (never applied silently: an
#: agent should learn the real name, not rely on a guess).
_SYSTEM_ALIASES = {
    "SNOMED": "SNOMEDCT_US",
    "SNOMEDCT": "SNOMEDCT_US",
    "SNOMEDCTUS": "SNOMEDCT_US",
    "SCT": "SNOMEDCT_US",
    "ICD10": "ICD10CM",
    "ICD10CM": "ICD10CM",
    "LOINC": "LNC",
    "RXNORM": "RXNORM",
}


def _unknown_systems(systems: list[str] | tuple[str, ...], name: str) -> dict[str, Any] | None:
    unknown = sorted(set(systems) - set(CODE_SYSTEMS))
    if not unknown:
        return None
    error = f"unknown {name} {unknown}; choose from {list(CODE_SYSTEMS)}"
    hints = {
        u: alias
        for u in unknown
        if (alias := _SYSTEM_ALIASES.get("".join(ch for ch in u.upper() if ch.isalnum())))
    }
    if hints:
        error += " (did you mean " + ", ".join(f"{a} for {u}" for u, a in hints.items()) + "?)"
    return {"error": error}


def _shown(value: Any) -> Any:
    return _NOT_ASSESSED if value is None else value


def summarize(
    result: DocumentResult, *, max_concepts: int = 50, include_offsets: bool = False
) -> dict[str, Any]:
    """One entry per distinct CUI, with its assertion combinations and preferred codes."""
    by_cui: dict[str, dict[str, Any]] = {}
    combos: dict[str, Counter[tuple[Any, ...]]] = {}
    for m in result.mentions:
        entry = by_cui.setdefault(
            m.cui,
            {
                "cui": m.cui,
                "name": m.preferred_name,
                "group": m.semantic_group,
                "mentions": 0,
                "codes": [
                    {"system": c.system, "code": c.code, "display": c.display}
                    | ({"crosswalk": True} if c.is_crosswalk else {})
                    for c in m.codes
                    if c.is_preferred
                ],
            },
        )
        entry["mentions"] += 1
        a = m.assertions
        combos.setdefault(m.cui, Counter())[(a.negated, a.subject, a.history_of, a.uncertain)] += 1
        if include_offsets:
            entry.setdefault("spans", []).append([m.start, m.end])
    concepts = []
    for cui, entry in by_cui.items():
        entry["assertions"] = [
            {
                "negated": _shown(neg),
                "subject": _shown(subj),
                "history_of": _shown(hist),
                "uncertain": _shown(unc),
                "count": n,
            }
            for (neg, subj, hist, unc), n in combos[cui].most_common()
        ]
        concepts.append(entry)
    concepts.sort(key=lambda e: (-e["mentions"], e["name"]))
    return {
        "concepts": concepts[:max_concepts],
        "total_concepts": len(concepts),
        "truncated": len(concepts) > max_concepts,
    }


def build_server(
    config: ExtractionConfig | None = None, *, extractor: Extractor | None = None
) -> MCPServer:
    cfg = config or load_config()
    ex = extractor or Extractor(cfg)
    lock = threading.Lock()  # engines are not thread-safe

    server = MCPServer(
        name="cuiflow",
        version=__version__,
        instructions=(
            "Extract UMLS concepts, with SNOMED CT, RxNorm, ICD-10-CM and LOINC codes, from "
            "clinical text. An assertion shown as 'not assessed' was not evaluated; do not read "
            "it as false. ICD-10-CM codes are crosswalk candidates, not billing codes."
        ),
    )

    @server.tool(annotations=READ_ONLY)
    @_contained
    def extract_clinical_concepts(
        text: str,
        code_systems: list[str] | None = None,
        max_concepts: int = 50,
        include_offsets: bool = False,
    ) -> dict[str, Any]:
        """Extract clinical concepts from a note: one entry per distinct UMLS concept, with
        semantic group, assertion status (negated, subject, history_of, uncertain) and
        preferred codes. Uses the server's configured engine mode."""
        if len(text) > MAX_CHARS:
            return {"error": f"text is {len(text)} characters; the limit is {MAX_CHARS}"}
        systems = tuple(code_systems) if code_systems is not None else DEFAULT_CODE_SYSTEMS
        invalid = _out_of_range("max_concepts", max_concepts, MAX_CONCEPTS) or _unknown_systems(
            systems, "code systems"
        )
        if invalid:
            return invalid
        with lock:
            result = ex.process(text, "mcp")
            if ex.store is not None:
                result = _with_codes(ex, result, systems)
        out = summarize(result, max_concepts=max_concepts, include_offsets=include_offsets)
        out["mode"] = cfg.mode.value
        if ex.store is None:
            out["codes_note"] = "no terminology database configured; codes are unavailable"
        return out

    @server.tool(annotations=READ_ONLY)
    @_contained
    def lookup_cui(
        cui: str, include_ancestors: bool = False, ancestor_depth: int = 3
    ) -> dict[str, Any]:
        """Codes for a UMLS CUI in SNOMED CT, RxNorm, ICD-10-CM and LOINC. With
        include_ancestors, each preferred ICD-10-CM / SNOMED CT code also lists its ancestor
        codes up to ancestor_depth levels (1 = parents). RxNorm products list their
        ingredients."""
        if ex.store is None:
            return {"error": "no terminology database configured"}
        if include_ancestors and (
            invalid := _out_of_range("ancestor_depth", ancestor_depth, MAX_ANCESTOR_DEPTH)
        ):
            return invalid
        store = ex.store
        out: dict[str, Any] = {"cui": cui}
        with lock:
            codes = store.codes_for(cui)
            out["display"] = store.preferred_display(cui)
            if include_ancestors:
                if not store.hierarchy_sources:
                    out["ancestors_note"] = "the terminology database has no hierarchy"
                out["ancestors"] = {
                    f"{c.system}:{c.code}": [
                        {"code": a, "distance": d}
                        for a, d in store.ancestors(c.system, c.code, max_distance=ancestor_depth)
                    ]
                    for c in codes
                    if c.is_preferred and c.system in store.hierarchy_sources
                }
            if store.has_ingredients and any(c.system == "RXNORM" for c in codes):
                out["ingredients"] = [i._asdict() for i in store.ingredients(cui)]
        out["codes"] = [c.to_dict() for c in codes]
        return out

    @server.tool(annotations=READ_ONLY)
    @_contained
    def crosswalk_medical_code(
        source_system: str, code: str, target_systems: list[str]
    ) -> dict[str, Any]:
        """Translate a code to other vocabularies by way of its shared UMLS CUI. This is UMLS
        co-occurrence, not a curated map."""
        if ex.store is None:
            return {"error": "no terminology database configured"}
        # An unknown source system would otherwise find no CUIs and look like "no crosswalk".
        if invalid := _unknown_systems([source_system], "source system"):
            return invalid
        if invalid := _unknown_systems(target_systems, "target systems"):
            return invalid
        with lock:
            cuis = ex.store.cuis_for_code(source_system, code)
            targets = {
                cui: [c.to_dict() for c in ex.store.codes_for(cui, target_systems)] for cui in cuis
            }
        return {
            "source": {"system": source_system, "code": code},
            "via_cuis": targets,
            "note": "UMLS co-occurrence, not a curated map",
        }

    @server.tool(annotations=READ_ONLY)
    @_contained
    def search_medical_terms(query: str, max_results: int = 10) -> dict[str, Any]:
        """Normalized dictionary lookup of a term (not fuzzy search). Needs the mmlite engine."""
        lookup = getattr(ex.engine, "lookup_term", None)
        if lookup is None:
            return {"error": "term search needs mode single:mmlite"}
        if len(query) > MAX_QUERY_CHARS:
            return {"error": f"query is {len(query)} characters; the limit is {MAX_QUERY_CHARS}"}
        if invalid := _out_of_range("max_results", max_results, MAX_RESULTS):
            return invalid
        with lock:
            hits = lookup(query)
        return {"query": query, "results": [{"cui": c, "name": n} for c, n in hits[:max_results]]}

    @server.resource("cuiflow://status", mime_type="application/json")
    def status() -> dict[str, Any]:
        """Version, configuration and loaded indexes."""
        with lock:
            info = ex.info()
        return {"cuiflow": __version__, "config": cfg.to_dict(), **info}

    @server.resource("cuiflow://semantic-groups", mime_type="application/json")
    def semantic_groups() -> list[str]:
        """The cTAKES semantic group names mentions are labelled with."""
        from umlsmatch.umls.semantic_tui import SemanticGroup

        return [g.name for g in SemanticGroup]

    return server


ASGIApp = Callable[[dict[str, Any], Callable[..., Any], Callable[..., Any]], Awaitable[None]]


class BearerAuth:
    """ASGI middleware: every connection but the server's own ``lifespan`` needs
    ``Authorization: Bearer <token>``. An HTTP request without it gets 401, a websocket is
    closed before it is accepted, and any other scope is dropped.

    Plain ASGI rather than a Starlette middleware, so streamed (SSE) responses pass through
    unbuffered."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(
        self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]
    ) -> None:
        if scope["type"] != "lifespan":
            headers = dict(scope.get("headers") or [])
            supplied = headers.get(b"authorization", b"").decode("latin-1")
            if bearer_matches(supplied, self.token):
                await self.app(scope, receive, send)
            elif scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            elif scope["type"] == "http":
                body = b'{"error":"invalid or missing bearer token"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b"Bearer"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def http_app(
    server: MCPServer, *, token: str | None, host: str = "127.0.0.1", path: str = "/mcp"
) -> ASGIApp:
    """The streamable HTTP app, behind the bearer token when one is given."""
    app: ASGIApp = server.streamable_http_app(streamable_http_path=path, host=host)
    return BearerAuth(app, token) if token else app


def _with_codes(ex: Extractor, result: DocumentResult, systems: tuple[str, ...]) -> DocumentResult:
    import dataclasses

    assert ex.store is not None
    store = ex.store
    mentions = tuple(
        dataclasses.replace(m, codes=store.codes_for(m.cui, systems, preferred_only=True))
        for m in result.mentions
    )
    return dataclasses.replace(result, mentions=mentions)
