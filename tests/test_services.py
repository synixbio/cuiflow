"""REST and MCP interfaces over a fake engine. Skipped when their extras are not installed."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cuiflow.api import Extractor
from cuiflow.core.config import ExtractionConfig
from tests.conftest import FakeEngine


def test_rest_endpoints_and_auth(
    patch_extractor: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import cuiflow.api
    import cuiflow.interfaces.rest as rest

    patch_extractor()
    monkeypatch.setattr(rest, "Extractor", cuiflow.api.Extractor)  # the patched factory
    monkeypatch.setenv("CUIFLOW_API_TOKEN", "s3cret")
    with TestClient(rest.create_app(ExtractionConfig())) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/extract", json={"text": "No chest pain."}).status_code == 401
        r = client.post(
            "/extract",
            json={"text": "No chest pain.", "doc_id": "d1"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert r.status_code == 200
        assert r.json()["doc_id"] == "d1" and r.json()["mentions"]


def test_mcp_summary_and_tools() -> None:
    pytest.importorskip("mcp")
    from cuiflow.interfaces.mcp_server import build_server, summarize

    ex = Extractor(ExtractionConfig(), engine=FakeEngine())
    result = ex.process("No chest pain. Chest pain again.", "d")
    summary = summarize(result, max_concepts=1)
    assert summary["truncated"] is True and summary["total_concepts"] == 2
    (top,) = summary["concepts"]
    assert top["cui"] == "C0008031" or top["mentions"] >= 1
    assert top["assertions"][0]["history_of"] == "not assessed"  # None is shown, not hidden

    server = build_server(ExtractionConfig(), extractor=ex)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == {
        "extract_clinical_concepts",
        "lookup_cui",
        "crosswalk_medical_code",
        "search_medical_terms",
    }
    assert all(t.annotations and t.annotations.read_only_hint for t in tools.values())


def test_mcp_unknown_code_system_is_an_error_with_a_hint() -> None:
    pytest.importorskip("mcp")
    from cuiflow.interfaces.mcp_server import _unknown_systems

    assert _unknown_systems(["SNOMEDCT_US"], "source system") is None
    error = _unknown_systems(["SNOMED CT"], "source system")
    assert error is not None and "SNOMEDCT_US for SNOMED CT" in error["error"]
    error = _unknown_systems(["ICD-10-CM", "LOINC", "MeSH"], "target systems")
    assert error is not None
    assert "ICD10CM for ICD-10-CM, LNC for LOINC?" in error["error"] and "MSH" not in error["error"]


def test_rest_metrics_carry_no_request_content(
    patch_extractor: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("prometheus_client")
    from fastapi.testclient import TestClient

    import cuiflow.api
    import cuiflow.interfaces.rest as rest

    patch_extractor()
    monkeypatch.setattr(rest, "Extractor", cuiflow.api.Extractor)
    monkeypatch.setenv("CUIFLOW_API_TOKEN", "s3cret")
    auth = {"Authorization": "Bearer s3cret"}
    with TestClient(rest.create_app(ExtractionConfig(), pool_size=2)) as client:
        assert client.get("/metrics").status_code == 401  # behind the token like the rest
        for _ in range(3):
            client.post(
                "/extract",
                json={"text": "SECRET-NOTE chest pain", "doc_id": "MRN123"},
                headers=auth,
            )
        client.get("/concepts/C0008031", headers=auth)  # 404: no terminology database
        body = client.get("/metrics", headers=auth).text

    assert 'cuiflow_http_requests_total{method="POST",route="/extract",status="200"} 3.0' in body
    assert 'route="/concepts/{cui}",status="404"' in body  # the template, not the CUI
    assert "cuiflow_documents_total 3.0" in body
    assert "cuiflow_mentions_total 6.0" in body  # "chest pain" and "pain" per document
    assert "cuiflow_pool_size 2.0" in body
    assert 'cuiflow_info{mode="single:umlsmatch",profile="strict"' in body
    for leaked in ("SECRET", "MRN123", "C0008031"):
        assert leaked not in body


# An MCP server over real stdio: the FakeEngine, and a store built from the synthetic RRF rows.
MCP_CHILD = """
import sys
from cuiflow.api import Extractor
from cuiflow.core.config import ExtractionConfig
from cuiflow.interfaces.mcp_server import build_server
from tests.conftest import FakeEngine

cfg = ExtractionConfig(terminology_db=sys.argv[1])
build_server(cfg, extractor=Extractor(cfg, engine=FakeEngine())).run(transport="stdio")
"""


def test_mcp_client_calls_every_tool_over_stdio(terminology_db: Path) -> None:
    pytest.importorskip("mcp")
    import json

    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command=sys.executable, args=["-c", MCP_CHILD, str(terminology_db)], cwd=str(root)
    )

    async def session() -> dict[str, Any]:
        out: dict[str, Any] = {}
        async with Client(params, read_timeout_seconds=60) as client:
            tools = await client.list_tools()
            out["tools"] = {t.name for t in tools.tools}
            calls = {
                "extract_clinical_concepts": {"text": "No chest pain. Takes metformin."},
                "lookup_cui": {"cui": "C0008031", "include_ancestors": True},
                "crosswalk_medical_code": {
                    "source_system": "ICD10CM",
                    "code": "R07.9",
                    "target_systems": ["SNOMEDCT_US"],
                },
                "search_medical_terms": {"query": "chest pain"},
            }
            for name, args in calls.items():
                result = await client.call_tool(name, args)
                assert not result.is_error, (name, result)
                out[name] = result.structured_content or json.loads(result.content[0].text)
            bad = await client.call_tool(
                "crosswalk_medical_code",
                {"source_system": "SNOMED", "code": "29857009", "target_systems": ["ICD10CM"]},
            )
            out["bad_source"] = bad.structured_content or json.loads(bad.content[0].text)
            for uri in ("cuiflow://status", "cuiflow://semantic-groups"):
                res = await client.read_resource(uri)
                out[uri] = json.loads(res.contents[0].text)
        return out

    out = asyncio.run(session())
    assert out.pop("bad_source")["error"].endswith("(did you mean SNOMEDCT_US for SNOMED?)")
    assert out["tools"] == set(out) - {"tools", "cuiflow://status", "cuiflow://semantic-groups"}
    concepts = {c["cui"]: c for c in out["extract_clinical_concepts"]["concepts"]}
    chest = concepts["C0008031"]
    assert chest["assertions"][0]["negated"] is True
    assert chest["codes"] == [
        {
            "system": "ICD10CM",
            "code": "R07.9",
            "display": "Chest pain, unspecified",
            "crosswalk": True,
        },
        {"system": "SNOMEDCT_US", "code": "29857009", "display": "Chest pain"},
    ]
    lookup = out["lookup_cui"]
    assert lookup["display"] == "Chest pain"  # preferred rows, SNOMEDCT_US first
    assert lookup["ancestors_note"] == "the terminology database has no hierarchy"
    assert out["crosswalk_medical_code"]["via_cuis"]["C0008031"][0]["code"] == "29857009"
    assert "error" in out["search_medical_terms"]  # term search needs single:mmlite: says so
    assert out["cuiflow://status"]["terminology"]["umls_release"] == "2026AA"
    assert "DISORDER" in out["cuiflow://semantic-groups"]


MCP_HTTP_CHILD = """
import sys
import uvicorn
from cuiflow.api import Extractor
from cuiflow.core.config import ExtractionConfig
from cuiflow.interfaces.mcp_server import build_server, http_app
from tests.conftest import FakeEngine

cfg = ExtractionConfig(terminology_db=sys.argv[1])
server = build_server(cfg, extractor=Extractor(cfg, engine=FakeEngine()))
uvicorn.run(http_app(server, token="s3cret"), host="127.0.0.1", port=int(sys.argv[2]),
            log_level="warning")
"""


def test_mcp_client_over_streamable_http_needs_the_token(terminology_db: Path) -> None:
    pytest.importorskip("mcp")
    import json
    import socket
    import subprocess
    import time

    import httpx
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    child = subprocess.Popen(
        [sys.executable, "-c", MCP_HTTP_CHILD, str(terminology_db), str(port)], cwd=str(root)
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                r = httpx.post(url, json={})
                break
            except httpx.TransportError:
                assert time.monotonic() < deadline, "server did not start"
                time.sleep(0.2)
        assert r.status_code == 401
        assert httpx.post(url, json={}, headers={"Authorization": "Bearer nope"}).status_code == 401

        async def session() -> dict[str, Any]:
            http = create_mcp_http_client(headers={"Authorization": "Bearer s3cret"})
            async with (
                http,
                streamable_http_client(url, http_client=http) as (read, write),
                ClientSession(read, write) as client,
            ):
                await client.initialize()
                tools = await client.list_tools()
                result = await client.call_tool(
                    "extract_clinical_concepts", {"text": "No chest pain."}
                )
                assert not result.is_error, result
                return {
                    "tools": {t.name for t in tools.tools},
                    "extract": result.structured_content or json.loads(result.content[0].text),
                }

        out = asyncio.run(session())
    finally:
        child.terminate()
        child.wait(timeout=30)
    assert "extract_clinical_concepts" in out["tools"] and len(out["tools"]) == 4
    (chest,) = [c for c in out["extract"]["concepts"] if c["cui"] == "C0008031"]
    assert chest["assertions"][0]["negated"] is True


def test_mcp_cli_refuses_open_host_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp")
    from cuiflow.interfaces import cli, mcp_server

    monkeypatch.delenv("CUIFLOW_API_TOKEN", raising=False)
    monkeypatch.setattr(mcp_server, "build_server", lambda cfg: object())
    assert cli.main(["mcp", "--transport", "http", "--host", "0.0.0.0"]) == 2


def test_rest_busy_pool_is_503_for_extraction_only(
    patch_extractor: Callable[..., None], monkeypatch: pytest.MonkeyPatch, terminology_db: Path
) -> None:
    pytest.importorskip("fastapi")
    from contextlib import contextmanager

    from fastapi.testclient import TestClient

    import cuiflow.api
    import cuiflow.interfaces.rest as rest
    from cuiflow.interfaces.pool import ExtractorPool, PoolExhausted

    @contextmanager
    def busy(self: ExtractorPool) -> Any:
        raise PoolExhausted("no extractor free")
        yield

    patch_extractor()
    monkeypatch.setattr(rest, "Extractor", cuiflow.api.Extractor)
    monkeypatch.setattr(ExtractorPool, "lease", busy)
    monkeypatch.delenv("CUIFLOW_API_TOKEN", raising=False)
    with TestClient(rest.create_app(ExtractionConfig(terminology_db=terminology_db))) as client:
        # Probes, /info and code lookups need no extractor, so a saturated pool does not fail
        # them.
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
        assert client.get("/info").json()["engine"]
        concept = client.get("/concepts/C0008031")
        assert concept.status_code == 200 and concept.json()["codes"]
        r = client.post("/extract", json={"text": "No chest pain."})
        assert r.status_code == 503 and r.headers["Retry-After"] == "1"
        # Bounded inputs: the ancestor walk and the document key.
        assert client.get("/concepts/C0008031?ancestor_depth=11").status_code == 422
        assert client.get("/concepts/C0008031?ancestor_depth=-1").status_code == 422
        long_id = {"text": "x", "doc_id": "d" * 1001}
        assert client.post("/extract", json=long_id).status_code == 422


def test_rest_ready_is_a_probe(
    patch_extractor: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import cuiflow.api
    import cuiflow.interfaces.rest as rest

    patch_extractor()
    monkeypatch.setattr(rest, "Extractor", cuiflow.api.Extractor)
    monkeypatch.setenv("CUIFLOW_API_TOKEN", "s3cret")
    app = rest.create_app(ExtractionConfig())
    r = TestClient(app).get("/ready")  # no lifespan: the pool was never built
    assert r.status_code == 503 and r.json() == {"ready": False}
    with TestClient(app) as client:
        r = client.get("/ready")  # no token needed, like /health
        assert r.status_code == 200 and r.json() == {"ready": True}


def test_rest_extract_failure_names_only_the_type(
    patch_extractor: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import cuiflow.api
    import cuiflow.interfaces.rest as rest

    patch_extractor(fail_on="SECRET")
    monkeypatch.setattr(rest, "Extractor", cuiflow.api.Extractor)
    monkeypatch.delenv("CUIFLOW_API_TOKEN", raising=False)
    with TestClient(rest.create_app(ExtractionConfig())) as client:
        r = client.post("/extract", json={"text": "SECRET-NOTE chest pain"})
    assert r.status_code == 500
    assert r.json() == {"detail": "extraction failed: RuntimeError"}
    assert "SECRET" not in caplog.text


def test_serve_refuses_open_host_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fastapi")
    import uvicorn

    from cuiflow.interfaces import cli

    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not start")

    monkeypatch.delenv("CUIFLOW_API_TOKEN", raising=False)
    monkeypatch.setattr(uvicorn, "run", never)
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2


def test_bearer_matches_needs_the_scheme() -> None:
    from cuiflow.interfaces import bearer_matches

    assert bearer_matches("Bearer s3cret", "s3cret")
    assert bearer_matches("bearer  s3cret ", "s3cret")
    assert not bearer_matches("s3cret", "s3cret")  # a bare token is not a bearer credential
    assert not bearer_matches("Basic s3cret", "s3cret")
    assert not bearer_matches(None, "s3cret")
    assert not bearer_matches("Bearer nope", "s3cret")


def test_rest_rejects_a_bare_token(
    patch_extractor: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import cuiflow.api
    import cuiflow.interfaces.rest as rest

    patch_extractor()
    monkeypatch.setattr(rest, "Extractor", cuiflow.api.Extractor)
    monkeypatch.setenv("CUIFLOW_API_TOKEN", "s3cret")
    with TestClient(rest.create_app(ExtractionConfig())) as client:
        assert client.get("/info", headers={"Authorization": "s3cret"}).status_code == 401
        assert client.get("/info", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_mcp_bearer_auth_guards_every_scope_but_lifespan() -> None:
    pytest.importorskip("mcp")
    from cuiflow.interfaces.mcp_server import BearerAuth

    reached: list[str] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        reached.append(scope["type"])

    async def run(scope: dict[str, Any]) -> list[dict[str, Any]]:
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await BearerAuth(app, "s3cret")(scope, None, send)
        return sent

    ok = [(b"authorization", b"Bearer s3cret")]
    assert asyncio.run(run({"type": "lifespan"})) == []
    (close,) = asyncio.run(run({"type": "websocket", "headers": []}))
    assert close == {"type": "websocket.close", "code": 1008}
    start, _ = asyncio.run(run({"type": "http", "headers": [(b"authorization", b"s3cret")]}))
    assert start["status"] == 401
    asyncio.run(run({"type": "websocket", "headers": ok}))
    asyncio.run(run({"type": "http", "headers": ok}))
    assert reached == ["lifespan", "websocket", "http"]


def test_mcp_tools_refuse_arguments_out_of_bounds() -> None:
    pytest.importorskip("mcp")
    from cuiflow.interfaces import MAX_CHARS
    from cuiflow.interfaces.mcp_server import build_server

    ex = Extractor(ExtractionConfig(), engine=FakeEngine())
    server = build_server(ExtractionConfig(), extractor=ex)

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        result = asyncio.run(server.call_tool(name, args))
        return result.structured_content  # type: ignore[no-any-return]

    text = "No chest pain."
    for args, message in (
        ({"text": "x" * (MAX_CHARS + 1)}, "the limit is"),
        ({"text": text, "max_concepts": -1}, "max_concepts must be between 1 and"),
        ({"text": text, "max_concepts": 0}, "max_concepts must be between 1 and"),
        ({"text": text, "code_systems": ["SNOMED"]}, "unknown code systems ['SNOMED']"),
    ):
        out = call("extract_clinical_concepts", args)
        assert message in out["error"], args
    assert "concepts" in call("extract_clinical_concepts", {"text": text, "max_concepts": 1})


def test_mcp_tool_failure_names_only_the_type(caplog: pytest.LogCaptureFixture) -> None:
    pytest.importorskip("mcp")
    from cuiflow.interfaces.mcp_server import build_server

    ex = Extractor(ExtractionConfig(), engine=FakeEngine(fail_on="SECRET"))
    server = build_server(ExtractionConfig(), extractor=ex)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert "text" in tools["extract_clinical_concepts"].input_schema["properties"]
    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(
            server.call_tool("extract_clinical_concepts", {"text": "SECRET patient note"})
        )
    assert result.structured_content == {"error": "internal error (RuntimeError)"}
    assert "RuntimeError" in caplog.text and "SECRET" not in caplog.text
