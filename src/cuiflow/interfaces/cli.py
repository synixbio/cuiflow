"""``cuiflow`` / ``python -m cuiflow``.

argparse rather than a CLI framework, so the core install stays dependency-free.

    cuiflow extract note.txt                       # JSONL to stdout
    cuiflow extract notes/ --out-dir out/runs      # run directory + manifest
    cuiflow extract notes/ -o results.jsonl
    cuiflow build-terminology --mrconso M/MRCONSO.RRF --mrrank M/MRRANK.RRF --mrrel M/MRREL.RRF
    cuiflow info
    cuiflow serve                                  # REST (needs the 'server' extra)
    cuiflow mcp                                    # MCP over stdio (needs the 'mcp' extra)
    cuiflow mcp --transport http --port 8765       # MCP over streamable HTTP
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from cuiflow.core.config import PROFILES, ExtractionConfig, load_config
from cuiflow.core.enums import CODE_SYSTEMS, ConflictPolicy, EngineMode, OverlapPolicy
from cuiflow.evaluation.gold import GUIDELINES_VERSION


def _add_config_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("configuration (overrides CUIFLOW_* and cuiflow.toml)")
    g.add_argument("--config", type=Path, help="config file (default: ./cuiflow.toml if present)")
    g.add_argument("--mode", choices=[m.value for m in EngineMode])
    g.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        help="preset for mode and overlaps (high_precision is proposed, not yet measured)",
    )
    g.add_argument("--overlaps", choices=[o.value for o in OverlapPolicy])
    g.add_argument("--conflict-policy", choices=[c.value for c in ConflictPolicy])
    g.add_argument("--groups", help="semantic groups to keep, comma-separated (e.g. DISORDER,DRUG)")
    g.add_argument(
        "--mention-filter",
        choices=["none", "guidelines"],
        help="post-filter engine output with the annotation guidelines' rules",
    )
    g.add_argument(
        "--codes", help=f"code systems to attach, comma-separated: {','.join(CODE_SYSTEMS)}"
    )
    g.add_argument(
        "--ancestors",
        type=int,
        metavar="N",
        help="attach ICD-10-CM/SNOMED CT ancestors up to distance N (store built with --mrrel)",
    )
    g.add_argument(
        "--ingredients",
        action="store_true",
        default=None,
        help="attach RxNorm ingredient RxCUIs (store built with --mrrel)",
    )
    g.add_argument("--terminology-db", type=Path)
    g.add_argument("--mmlite-index", type=Path)
    g.add_argument("--mmlite-negation", choices=["negex", "context"])
    g.add_argument("--umlsmatch-db", type=Path)
    g.add_argument(
        "--include-text", action="store_true", default=None, help="include note text (PHI)"
    )


def _config_from(args: argparse.Namespace) -> ExtractionConfig:
    overrides: dict[str, Any] = {
        "mode": args.mode,
        "profile": args.profile,
        "overlaps": args.overlaps,
        "conflict_policy": args.conflict_policy,
        "semantic_groups": args.groups,
        "mention_filter": args.mention_filter,
        "code_systems": args.codes,
        "ancestor_depth": args.ancestors,
        "include_ingredients": args.ingredients,
        "terminology_db": args.terminology_db,
        "mmlite_index": args.mmlite_index,
        "mmlite_negation": args.mmlite_negation,
        "umlsmatch_db": args.umlsmatch_db,
        "include_text": args.include_text,
    }
    return load_config(overrides, config_file=args.config)


def _cmd_extract(args: argparse.Namespace) -> int:
    from cuiflow.io.readers import skipped_in_folders

    config = _config_from(args)
    if args.workers > 1 and not (args.out_dir or args.resume):
        raise ValueError("--workers needs --out-dir: a one-shot extract runs in one process")
    if args.timeout is not None and not (args.out_dir or args.resume):
        raise ValueError("--timeout needs --out-dir: a one-shot extract runs in one process")
    skipped = skipped_in_folders(args.inputs)
    if skipped:
        print(
            f"cuiflow: warning: {len(skipped)} files in the input folders are not .txt or .text "
            f"and are not read (e.g. {skipped[0].name}); name a .jsonl file directly",
            file=sys.stderr,
        )
    if args.out_dir or args.resume:
        from cuiflow.interfaces.batch import run_batch

        run_dir = run_batch(
            args.inputs,
            args.out_dir or Path("out/runs"),
            config,
            workers=args.workers,
            run_id=args.run_id,
            resume=args.resume,
            timeout=args.timeout,
        )
        manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        print(
            f"{run_dir}: {manifest['documents']} documents, {manifest['mentions']} mentions, "
            f"{len(manifest['errors'])} errors, {manifest['skipped_resumed']} skipped",
            file=sys.stderr,
        )
        if manifest.get("duplicate_doc_ids"):
            print(
                f"cuiflow: warning: {manifest['duplicate_doc_ids']} documents reuse an earlier "
                f"doc_id (e.g. {', '.join(manifest['duplicate_doc_id_examples'])}); "
                "results.jsonl has more than one record for those keys",
                file=sys.stderr,
            )
        if manifest.get("documents_with_invalid_utf8"):
            print(
                f"cuiflow: warning: {manifest['documents_with_invalid_utf8']} files are not "
                f"valid UTF-8 (e.g. {', '.join(manifest['invalid_utf8_examples'])}); their "
                "undecodable bytes were replaced with U+FFFD",
                file=sys.stderr,
            )
        return 1 if manifest["errors"] else 0

    from cuiflow.api import Extractor
    from cuiflow.core.manifests import ErrorRecord
    from cuiflow.core.models import DocumentResult
    from cuiflow.io.readers import iter_documents
    from cuiflow.io.writers import format_for, open_writer

    errors = 0

    def report(record: ErrorRecord) -> None:
        nonlocal errors
        errors += 1
        print(json.dumps({"error": record.to_dict()}), file=sys.stderr)

    def results(extractor: Extractor) -> Iterator[DocumentResult]:
        # A failing or unreadable document does not stop the rest. Like a batch run's error
        # records, the report names the exception type only: its message may quote the note.
        def unreadable(location: str, error_type: str) -> None:
            report(ErrorRecord(location, "read", error_type))

        for doc in iter_documents(args.inputs, on_error=unreadable):
            try:
                yield extractor.process(doc.text, doc.doc_id)
            except Exception as exc:
                report(ErrorRecord(doc.doc_id, "extract", type(exc).__name__))

    if args.output is None:
        with Extractor(config) as extractor:
            for result in results(extractor):
                sys.stdout.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
    else:
        # Written under another name and renamed once complete, as ``cuiflow export`` does, so
        # an interrupted run never leaves a file that looks finished.
        out: Path = args.output
        if out.exists():
            raise FileExistsError(f"{out} exists; remove it first or choose another path")
        fmt = format_for(out, args.format)
        partial = out.with_name(f".partial.{out.name}")
        partial.unlink(missing_ok=True)  # left by an earlier run that was killed
        try:
            writer = open_writer(partial, fmt)
            try:
                with Extractor(config) as extractor:
                    for result in results(extractor):
                        writer.write(result)
            finally:
                writer.close()
            partial.replace(out)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    if errors:
        print(f"{errors} documents failed", file=sys.stderr)
    return 1 if errors else 0


def _cmd_export(args: argparse.Namespace) -> int:
    from cuiflow.interfaces.export import export_results, load_note_ids, note_id_from_doc_id

    note_ids: Any = None
    if args.note_ids and args.note_id_from_doc_id:
        raise ValueError("give --note-ids or --note-id-from-doc-id, not both")
    if args.note_ids:
        note_ids = load_note_ids(args.note_ids).get
    elif args.note_id_from_doc_id:
        note_ids = note_id_from_doc_id
    summary = export_results(
        args.source,
        args.output,
        args.format,
        note_ids=note_ids,
        omop_vocab=args.omop_vocab,
        snippets=args.snippets,
        id_scheme=args.id_scheme,
        first_id=args.first_id,
    )
    print(
        f"{args.output}: {summary['documents']} documents, {summary['mentions']} mentions",
        file=sys.stderr,
    )
    if summary.get("missing_note_id"):
        print(
            f"error: {summary['missing_note_id']} document(s) had no note_id and were left out",
            file=sys.stderr,
        )
        return 1
    if summary["format"] == "omop" and args.omop_vocab is None:
        print("note: concept IDs are 0 without --omop-vocab", file=sys.stderr)
    return 0


def _cmd_build_omop_vocab(args: argparse.Namespace) -> int:
    from cuiflow.terminology.omop import build_omop_vocabulary

    def progress(stage: str, n: int) -> None:
        print(f"\r{stage}: {n:,} rows ", end="", file=sys.stderr)

    out = build_omop_vocabulary(args.athena, args.out, progress=progress)
    print(f"\nOMOP vocabulary database ready: {out}", file=sys.stderr)
    return 0


def _cmd_build_terminology(args: argparse.Namespace) -> int:
    from cuiflow.terminology.builder import build_terminology

    sources = tuple(s.strip() for s in args.sources.split(",")) if args.sources else CODE_SYSTEMS

    def progress(stage: str, n: int) -> None:
        print(f"\r{stage}: {n:,} rows ", end="", file=sys.stderr)

    out = build_terminology(
        args.mrconso,
        args.mrrank,
        args.out,
        mrrel=args.mrrel,
        sources=sources,
        include_suppressed=args.include_suppressed,
        hierarchy_max_depth=args.hierarchy_max_depth,
        umls_release=args.umls_release,
        progress=progress,
    )
    print(f"\nterminology database ready: {out}", file=sys.stderr)
    return 0


def _cmd_inspect_manifest(args: argparse.Namespace) -> int:
    from cuiflow.core.manifests import read_manifest

    print(json.dumps(read_manifest(args.run_dir), indent=2))
    return 0


def _report_problems(problems: Any) -> None:
    for w in problems.warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in problems.errors:
        print(f"error: {e}", file=sys.stderr)


def _cmd_gold_convert(args: argparse.Namespace) -> int:
    from cuiflow.evaluation.gold import convert_worksheet, write_jsonl

    records, problems = convert_worksheet(
        args.worksheet,
        args.notes,
        annotator=args.annotator,
        guidelines_version=args.guidelines_version,
        source=args.source,
    )
    _report_problems(problems)
    if problems.errors:
        print(f"{len(problems.errors)} error(s); nothing written", file=sys.stderr)
        return 1
    write_jsonl(records, args.out)
    n = sum(len(r["mentions"]) for r in records)
    print(f"{args.out}: {len(records)} notes, {n} mentions", file=sys.stderr)
    return 0


def _cmd_gold_validate(args: argparse.Namespace) -> int:
    from cuiflow.evaluation.gold import read_jsonl, validate_records

    problems = validate_records(read_jsonl(args.reference))
    _report_problems(problems)
    print(
        f"{args.reference}: {len(problems.errors)} error(s), {len(problems.warnings)} warning(s)",
        file=sys.stderr,
    )
    return 1 if problems.errors else 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    from cuiflow.evaluation.reference import load_reference
    from cuiflow.evaluation.runner import MAJOR_GROUPS, run_evaluation

    config = _config_from(args)
    reference = load_reference(args.reference, args.format, corpus=args.corpus)
    modes = [EngineMode(m.strip()) for m in args.modes.split(",")] if args.modes else [config.mode]
    groups: tuple[str, ...]
    if args.scope == "all":
        groups = ()
    elif args.scope:
        groups = tuple(g.strip() for g in args.scope.split(","))
    else:
        groups = MAJOR_GROUPS
    report = run_evaluation(reference, modes, config, groups=groups)

    markdown = report.to_markdown()
    if args.out:
        args.out.mkdir(parents=True, exist_ok=False)  # never overwrite an earlier report
        (args.out / "eval_report.json").write_text(
            json.dumps(report.to_dict(per_document=args.per_document), indent=2), encoding="utf-8"
        )
        (args.out / "eval_report.md").write_text(markdown, encoding="utf-8")
        print(f"report written to {args.out}", file=sys.stderr)
    print(markdown)
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    from cuiflow import __version__

    config = _config_from(args)
    info: dict[str, Any] = {"cuiflow": __version__, "config": config.to_dict()}
    if args.load:
        from cuiflow.api import Extractor

        with Extractor(config) as extractor:
            info.update(extractor.info())
    print(json.dumps(info, indent=2))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("serve needs the 'server' extra: pip install 'cuiflow[server]'", file=sys.stderr)
        return 2
    import os

    from cuiflow.interfaces import LOOPBACK, TOKEN_ENV
    from cuiflow.interfaces.rest import create_app

    if not os.environ.get(TOKEN_ENV) and args.host not in LOOPBACK:
        print(f"refusing to serve on {args.host} without {TOKEN_ENV} set", file=sys.stderr)
        return 2
    app = create_app(_config_from(args), pool_size=args.pool_size)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    try:
        from cuiflow.interfaces.mcp_server import build_server
    except ImportError:
        print("mcp needs the 'mcp' extra: pip install 'cuiflow[mcp]'", file=sys.stderr)
        return 2
    server = build_server(_config_from(args))
    if args.transport == "stdio":
        server.run(transport="stdio")
        return 0
    import os

    import uvicorn

    from cuiflow.interfaces import LOOPBACK, TOKEN_ENV
    from cuiflow.interfaces.mcp_server import http_app

    token = os.environ.get(TOKEN_ENV) or None
    if token is None and args.host not in LOOPBACK:
        print(f"refusing to serve MCP on {args.host} without {TOKEN_ENV} set", file=sys.stderr)
        return 2
    app = http_app(server, token=token, host=args.host)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    from cuiflow import __version__

    parser = argparse.ArgumentParser(
        prog="cuiflow",
        description="Clinical concept extraction and UMLS terminology mapping.",
    )
    parser.add_argument("--version", action="version", version=f"cuiflow {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("extract", help="extract concepts from notes")
    p.add_argument("inputs", nargs="+", help="text files, folders, .jsonl files, or - for stdin")
    dest = p.add_mutually_exclusive_group()
    dest.add_argument(
        "-o",
        "--output",
        type=Path,
        help="write one file, never overwritten: .jsonl[.gz], .parquet or .sqlite",
    )
    dest.add_argument("--out-dir", type=Path, help="write a run directory with a manifest here")
    dest.add_argument("--resume", type=Path, help="resume an interrupted run directory")
    p.add_argument("--run-id", help="name the run directory instead of a timestamp")
    p.add_argument("--workers", type=int, default=1, help="worker processes (with --out-dir)")
    p.add_argument(
        "--timeout",
        type=float,
        metavar="SECONDS",
        help="record a document that takes longer as a Timeout error (with --out-dir)",
    )
    p.add_argument(
        "--format",
        choices=["jsonl", "parquet", "sqlite"],
        help="format for -o (default: from its suffix); OMOP output is made by 'export'",
    )
    _add_config_flags(p)
    p.set_defaults(func=_cmd_extract)

    p = sub.add_parser("export", help="convert a run's results to Parquet, SQLite or OMOP NOTE_NLP")
    p.add_argument("source", type=Path, help="a run directory, or a results .jsonl[.gz] file")
    p.add_argument("-o", "--output", type=Path, required=True, help="never overwritten")
    p.add_argument(
        "--format",
        choices=["jsonl", "parquet", "sqlite", "omop"],
        help="default: from the output suffix (.csv means omop)",
    )
    ids = p.add_argument_group("OMOP NOTE_NLP")
    ids.add_argument(
        "--note-ids", type=Path, help="CSV with doc_id,note_id columns: each document's NOTE row"
    )
    ids.add_argument(
        "--note-id-from-doc-id",
        action="store_true",
        help="use integer document keys (e.g. JSONL ids) as note_id",
    )
    ids.add_argument(
        "--omop-vocab", type=Path, help="database from build-omop-vocab (concept IDs are 0 without)"
    )
    ids.add_argument(
        "--snippets",
        action="store_true",
        help="fill snippet from the note text (PHI; needs a run made with --include-text)",
    )
    ids.add_argument(
        "--id-scheme",
        choices=["sequential", "hash63"],
        default="sequential",
        help="note_nlp_id: sequential fits v5.4's integer column (default); hash63 is stable "
        "across exports but needs bigint",
    )
    ids.add_argument(
        "--first-id",
        type=int,
        default=1,
        help="first sequential note_nlp_id (to append after existing rows)",
    )
    p.set_defaults(func=_cmd_export)

    p = sub.add_parser(
        "build-omop-vocab", help="build the OMOP concept lookup from an Athena download"
    )
    p.add_argument("--athena", type=Path, required=True, help="folder with CONCEPT.csv etc.")
    p.add_argument("--out", type=Path, default=Path("data/omop_vocab.sqlite"))
    p.set_defaults(func=_cmd_build_omop_vocab)

    p = sub.add_parser("build-terminology", help="build terminology.sqlite from UMLS files")
    p.add_argument("--mrconso", type=Path, required=True)
    p.add_argument("--mrrank", type=Path, required=True)
    p.add_argument(
        "--mrrel",
        type=Path,
        help="MRREL.RRF: adds the ICD-10-CM/SNOMED CT hierarchy and RxNorm ingredients",
    )
    p.add_argument(
        "--hierarchy-max-depth",
        type=int,
        metavar="N",
        help="stop the ancestor walk after N levels (default: walk to the roots)",
    )
    p.add_argument("--out", type=Path, default=Path("data/terminology.sqlite"))
    p.add_argument("--sources", help=f"SABs, comma-separated (default: {','.join(CODE_SYSTEMS)})")
    p.add_argument("--umls-release", help="e.g. 2026AA (default: guessed from the path)")
    p.add_argument("--include-suppressed", action="store_true")
    p.set_defaults(func=_cmd_build_terminology)

    p = sub.add_parser("inspect-manifest", help="print a run's manifest")
    p.add_argument("run_dir", type=Path)
    p.set_defaults(func=_cmd_inspect_manifest)

    p = sub.add_parser("evaluate", help="score engine modes against a reference set")
    p.add_argument("--reference", type=Path, required=True, help="reference file or directory")
    p.add_argument(
        "--format",
        required=True,
        choices=["cuiflow", "ctakes-silver", "mmlite-fixtures"],
        help="reference format (see cuiflow.evaluation.reference)",
    )
    p.add_argument("--corpus", type=Path, help="the .txt files (mmlite-fixtures only)")
    p.add_argument("--modes", help="comma-separated modes to compare (default: --mode)")
    p.add_argument(
        "--scope",
        help="semantic groups scored on both sides, comma-separated, or 'all' "
        "(default: DISORDER,FINDING,DRUG,PROCEDURE,ANATOMY)",
    )
    p.add_argument("--out", type=Path, help="write eval_report.json/.md to this new directory")
    p.add_argument("--per-document", action="store_true", help="include per-document scores")
    _add_config_flags(p)
    p.set_defaults(func=_cmd_evaluate)

    p = sub.add_parser("gold", help="build and check a human-annotated gold set")
    gold = p.add_subparsers(dest="gold_command", required=True)
    g = gold.add_parser("convert", help="annotation worksheet (CSV) + notes -> reference JSONL")
    g.add_argument("--worksheet", type=Path, required=True)
    g.add_argument("--notes", type=Path, required=True, help="folder of the annotated .txt notes")
    g.add_argument("--annotator", required=True, help="annotator ID recorded as the source")
    g.add_argument("--out", type=Path, required=True, help="JSONL to write (never overwritten)")
    g.add_argument(
        "--source", help="recorded source (default human:<annotator>), e.g. adjudicated:<who>"
    )
    g.add_argument("--guidelines-version", default=GUIDELINES_VERSION)
    g.set_defaults(func=_cmd_gold_convert)
    g = gold.add_parser("validate", help="check reference JSONL against the guidelines")
    g.add_argument("reference", type=Path)
    g.set_defaults(func=_cmd_gold_validate)

    p = sub.add_parser("info", help="show the effective configuration")
    p.add_argument("--load", action="store_true", help="also load the engines and report them")
    _add_config_flags(p)
    p.set_defaults(func=_cmd_info)

    p = sub.add_parser("serve", help="run the REST service")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--pool-size", type=int, default=1)
    _add_config_flags(p)
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("mcp", help="run the MCP server (stdio, or streamable HTTP)")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1", help="with --transport http")
    p.add_argument("--port", type=int, default=8765, help="with --transport http")
    _add_config_flags(p)
    p.set_defaults(func=_cmd_mcp)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code: int = args.func(args)
    except (
        ValueError,
        FileNotFoundError,
        FileExistsError,
        NotImplementedError,
        ImportError,  # a missing extra: the message names the pip install to run
    ) as exc:
        # Messages here come from configuration and paths, never from note text.
        print(f"cuiflow: {exc}", file=sys.stderr)
        return 2
    return code
