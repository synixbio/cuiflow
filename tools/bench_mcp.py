#!/usr/bin/env python
"""The Phase 5 latency gate: MCP ``extract_clinical_concepts`` over stdio (DESIGN_PLAN §11).

Starts ``cuiflow mcp`` as a subprocess, exactly as an agent host would, and times it from the
client side:

- **startup**: spawn to a completed ``initialize`` (the engines load before the server answers);
- **first call**: the first extraction, which may pay for lazy loading;
- **warm calls**: ``--calls`` more extractions, each of a different typical synthetic note (a
  repeated note could be served from the engines' caches). The gate is a warm p95 within
  200 ms in the ``single:*`` modes.

    python tools/bench_mcp.py --mode single:umlsmatch --umlsmatch-db <db> \\
        --terminology-db data/terminology.sqlite

Extra flags after ``--`` go to ``cuiflow mcp`` unchanged. The note is invented
(``tools/synth_corpus.py``); never benchmark on real notes outside PHI controls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from synth_corpus import notes

GATE_MS = 200.0


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


async def bench(server_args: list[str], texts: list[str]) -> dict[str, Any]:
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "cuiflow", "mcp", *server_args]
    )
    t0 = time.perf_counter()
    async with Client(params, read_timeout_seconds=600) as client:
        startup = time.perf_counter() - t0
        t = time.perf_counter()
        first = await client.call_tool("extract_clinical_concepts", {"text": texts[0]})
        first_s = time.perf_counter() - t
        if first.is_error:
            raise RuntimeError(f"extract_clinical_concepts failed: {first.content}")
        warm: list[float] = []
        for text in texts[1:]:
            t = time.perf_counter()
            result = await client.call_tool("extract_clinical_concepts", {"text": text})
            warm.append((time.perf_counter() - t) * 1000)
            if result.is_error:
                raise RuntimeError(f"extract_clinical_concepts failed: {result.content}")
        summary = first.structured_content or json.loads(first.content[0].text)
    return {
        "startup_s": round(startup, 2),
        "first_call_ms": round(first_s * 1000, 1),
        "warm_calls": len(warm),
        "warm_p50_ms": round(statistics.median(warm), 1),
        "warm_p95_ms": round(_pct(warm, 95), 1),
        "warm_max_ms": round(max(warm), 1),
        "first_note_concepts": summary.get("total_concepts"),
        "mode": summary.get("mode"),
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1 :]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", required=True)
    ap.add_argument("--mmlite-index")
    ap.add_argument("--umlsmatch-db")
    ap.add_argument("--terminology-db")
    ap.add_argument("--codes", default="SNOMEDCT_US,RXNORM,ICD10CM")
    ap.add_argument("--calls", type=int, default=50)
    ap.add_argument("--seed", type=int, default=7, help="which synthetic notes")
    args = ap.parse_args(argv)

    server_args = ["--mode", args.mode]
    for flag in ("mmlite_index", "umlsmatch_db", "terminology_db"):
        if getattr(args, flag):
            server_args += [f"--{flag.replace('_', '-')}", getattr(args, flag)]
    if args.terminology_db:
        server_args += ["--codes", args.codes]
    server_args += passthrough

    texts = [text for _, text in notes(args.calls + 1, args.seed)]
    result = asyncio.run(bench(server_args, texts))
    result["note_chars_median"] = statistics.median(len(t) for t in texts)
    result["gate"] = (
        f"warm p95 <= {GATE_MS:.0f} ms: "
        + ("PASS" if result["warm_p95_ms"] <= GATE_MS else "FAIL")
        + ("" if args.mode.startswith("single:") else " (the gate applies to single:* modes)")
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
