#!/usr/bin/env python
"""Write an HTML page that shows each note with its mentions highlighted by assertion status.

Usage:
    python examples/highlight_note.py synthetic/doc_001.txt -o review.html
    python examples/highlight_note.py synthetic/ -o review.html --limit 5 --groups DISORDER DRUG
    python examples/highlight_note.py notes.jsonl -o review.html --mode single:mmlite

For reading extraction output against the note it came from: checking a mode before a study,
explaining a surprising count from ``notes_of_interest.py``, or spot-checking a run. Each mention
is colored by the status the other examples use:

- **affirmed**: not negated, about the patient, not hedged
- **negated**
- **someone else's**: ``subject`` is a family member or other
- **hedged**: uncertain, conditional, generic or hypothetical

Hover over a highlight for its concepts and assertions; overlapping mentions ("chest pain" and
the "pain" inside it) are underlined and all listed. The legend's checkboxes hide a status. A
table under each note lists every mention with its offsets, the attributes that were assessed
(an attribute left out was **not assessed**, which is not the same as false) and its preferred
codes, when the configuration asks for codes.

Only notes are read (files, folders, ``.jsonl``), since the page needs the text and a run keeps
it only when extracted with ``include_text``. ``--limit`` defaults to 20 notes: the page is for
reading, not for a corpus.

**The page contains the notes themselves.** It is PHI: keep it where the notes are allowed to
be, and do not open it on a machine they may not be on. It loads nothing from the network.

Exit status: 0 on success, 1 when any note failed, 2 on a usage or configuration error.
"""

from __future__ import annotations

import argparse
import html
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from cuiflow import Extractor, Mention, load_config
from cuiflow.core.enums import EngineMode
from cuiflow.core.models import ASSERTION_ATTRIBUTES
from cuiflow.io.readers import iter_documents

STATUSES = ("affirmed", "negated", "other_subject", "hedged")
LABELS = {
    "affirmed": "affirmed",
    "negated": "negated",
    "other_subject": "someone else's",
    "hedged": "hedged",
}
#: Whose preferred-code display names a concept, in order: an engine's preferred name can be any
#: synonym ("EHT" for essential hypertension). Without codes the engine's name is kept.
NAMING_SYSTEMS = ("SNOMEDCT_US", "RXNORM", "ICD10CM", "LNC")


def display_name(m: Mention) -> str:
    preferred = {c.system: c.display for c in m.codes if c.is_preferred}
    return next((preferred[s] for s in NAMING_SYSTEMS if s in preferred), m.preferred_name)


def mention_status(m: Mention) -> str:
    """The first bucket a mention fits. ``None`` (not assessed) never excludes a mention."""
    a = m.assertions
    if a.negated:
        return "negated"
    if a.subject not in (None, "patient"):
        return "other_subject"
    if a.uncertain or a.conditional or a.generic or a.temporality == "hypothetical":
        return "hedged"
    return "affirmed"


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    mentions: tuple[Mention, ...]  # covering this span, longest first


def segments(text: str, mentions: list[Mention]) -> Iterator[Segment]:
    """Split ``text`` at every mention boundary, so overlapping mentions never nest tags."""
    cuts = sorted({0, len(text), *(m.start for m in mentions), *(m.end for m in mentions)})
    for start, end in pairwise(cuts):
        if start >= end:
            continue
        covering = [m for m in mentions if m.start <= start and end <= m.end]
        covering.sort(key=lambda m: (-(m.end - m.start), m.start, m.cui))
        yield Segment(start, end, tuple(covering))


def describe(m: Mention) -> str:
    assessed = [
        f"{n}={getattr(m.assertions, n)}"
        for n in ASSERTION_ATTRIBUTES
        if getattr(m.assertions, n) is not None
    ]
    return f"{m.cui} {display_name(m)} ({m.semantic_group}): {LABELS[mention_status(m)]}" + (
        f" [{', '.join(assessed)}]" if assessed else " [nothing assessed]"
    )


def render_text(text: str, mentions: list[Mention]) -> str:
    out = []
    for seg in segments(text, mentions):
        chunk = html.escape(text[seg.start : seg.end])
        if not seg.mentions:
            out.append(chunk)
            continue
        status = mention_status(seg.mentions[0])
        nested = " nested" if len(seg.mentions) > 1 else ""
        title = html.escape("\n".join(describe(m) for m in seg.mentions), quote=True)
        out.append(f'<mark class="st-{status}{nested}" title="{title}">{chunk}</mark>')
    return "".join(out)


def render_table(mentions: list[Mention]) -> str:
    rows = []
    for m in mentions:
        assessed = ", ".join(
            f"{n}={getattr(m.assertions, n)}"
            for n in ASSERTION_ATTRIBUTES
            if getattr(m.assertions, n) is not None
        )
        codes = ", ".join(f"{c.system} {c.code}" for c in m.codes if c.is_preferred)
        status = mention_status(m)
        rows.append(
            f'<tr class="row-{status}"><td class="num">{m.start}&ndash;{m.end}</td>'
            f"<td>{html.escape(m.text)}</td><td><code>{m.cui}</code></td>"
            f"<td>{html.escape(display_name(m))}</td><td>{html.escape(m.semantic_group)}</td>"
            f'<td><span class="chip st-{status}">{LABELS[status]}</span></td>'
            f"<td>{html.escape(assessed) or '<em>none</em>'}</td>"
            f"<td>{html.escape(codes)}</td></tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Span</th><th>Text</th><th>CUI</th>'
        "<th>Concept</th><th>Group</th><th>Status</th><th>Assessed</th><th>Codes</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


CSS = """
:root {
  --bg: #fbfbf9; --fg: #1f2328; --muted: #656d76; --line: #d8dadd; --card: #ffffff;
  --affirmed: #c9ecd2; --affirmed-ink: #13612e;
  --negated: #f9d3d0; --negated-ink: #9a1c14;
  --other_subject: #d7e3fb; --other_subject-ink: #1d4a9e;
  --hedged: #fbe7b5; --hedged-ink: #7a5300;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #15171a; --fg: #e6e8eb; --muted: #9aa3ad; --line: #30353b; --card: #1c1f23;
    --affirmed: #1d4a2c; --affirmed-ink: #a6e3b8;
    --negated: #5a211d; --negated-ink: #f5b3ad;
    --other_subject: #1f3561; --other_subject-ink: #b3c9f5;
    --hedged: #574313; --hedged-ink: #f2d489;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 1.4rem; margin: 0 0 4px; }
.phi { color: var(--negated-ink); font-weight: 600; margin: 0 0 16px; }
.legend { position: sticky; top: 0; z-index: 1; background: var(--bg); padding: 10px 0;
  border-bottom: 1px solid var(--line); display: flex; flex-wrap: wrap; gap: 8px 16px; }
.legend label { cursor: pointer; display: inline-flex; align-items: center; gap: 6px; }
section { background: var(--card); border: 1px solid var(--line); border-radius: 8px;
  margin: 20px 0; padding: 16px; }
h2 { font-size: 1rem; margin: 0 0 8px; overflow-wrap: anywhere; }
.meta { color: var(--muted); font-size: 0.85rem; margin: 0 0 12px; }
.note { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0 0 12px;
  font: 13px/1.6 ui-monospace, "Cascadia Mono", Consolas, monospace; }
mark { color: inherit; border-radius: 2px; padding: 0 1px; }
mark.nested { text-decoration: underline; text-underline-offset: 3px; }
.st-affirmed { background: var(--affirmed); }
.st-negated { background: var(--negated); }
.st-other_subject { background: var(--other_subject); }
.st-hedged { background: var(--hedged); }
.chip { border-radius: 10px; padding: 0 8px; white-space: nowrap; font-size: 0.8rem; }
.chip.st-affirmed { color: var(--affirmed-ink); } .chip.st-negated { color: var(--negated-ink); }
.chip.st-other_subject { color: var(--other_subject-ink); }
.chip.st-hedged { color: var(--hedged-ink); }
body.hide-affirmed mark.st-affirmed, body.hide-negated mark.st-negated,
body.hide-other_subject mark.st-other_subject, body.hide-hedged mark.st-hedged {
  background: none; text-decoration: none; }
body.hide-affirmed tr.row-affirmed, body.hide-negated tr.row-negated,
body.hide-other_subject tr.row-other_subject, body.hide-hedged tr.row-hedged { display: none; }
details summary { cursor: pointer; color: var(--muted); }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 0.85rem; margin-top: 8px; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--line);
  vertical-align: top; }
td.num { white-space: nowrap; font-variant-numeric: tabular-nums; }
.error { color: var(--negated-ink); }
"""

SCRIPT = """
document.querySelectorAll('.legend input').forEach(function (box) {
  box.addEventListener('change', function () {
    document.body.classList.toggle('hide-' + box.value, !box.checked);
  });
});
"""


def render_page(sections: list[str], *, mode: str, n_notes: int) -> str:
    legend = "".join(
        f'<label><input type="checkbox" value="{s}" checked>'
        f'<span class="chip st-{s}">{LABELS[s]}</span></label>'
        for s in STATUSES
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Mention review</title><style>{CSS}</style></head><body><main>"
        f"<h1>Mention review</h1><p class='meta'>{n_notes} note(s), mode {html.escape(mode)}. "
        "Hover a highlight for its concepts; underlined text has overlapping mentions.</p>"
        '<p class="phi">Contains note text: PHI.</p>'
        f'<div class="legend">{legend}</div>{"".join(sections)}'
        f"</main><script>{SCRIPT}</script></body></html>\n"
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sources", nargs="+", type=Path, help="notes: files, folders or .jsonl")
    ap.add_argument("-o", "--output", type=Path, required=True, help="the HTML page to write")
    ap.add_argument("--groups", nargs="+", metavar="GROUP", help="only these semantic groups")
    ap.add_argument("--limit", type=int, default=20, help="at most this many notes (default 20)")
    cfg = ap.add_argument_group("configuration (overrides CUIFLOW_* and cuiflow.toml)")
    cfg.add_argument("--config", type=Path, help="config file (default: ./cuiflow.toml)")
    cfg.add_argument("--mode", choices=[m.value for m in EngineMode])
    cfg.add_argument("--mention-filter", choices=["none", "guidelines"])
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.limit < 1:
        ap.error("--limit must be at least 1")
    groups = {g.upper() for g in args.groups} if args.groups else None
    flags = {"mode": args.mode, "mention_filter": args.mention_filter}

    sections: list[str] = []
    errors = 0
    with ExitStack() as stack:
        # Messages caught here come from paths and configuration, never from note text:
        # errors on a note are caught per note below.
        try:
            for path in args.sources:
                if not path.exists():
                    raise FileNotFoundError(f"no such file or folder: {path}")
            config = load_config(
                {k: v for k, v in flags.items() if v is not None}, config_file=args.config
            )
            extractor = stack.enter_context(Extractor(config))
            for n, doc in enumerate(iter_documents(args.sources)):
                if n >= args.limit:
                    print(f"note: stopped after {args.limit} notes (--limit)", file=sys.stderr)
                    break
                heading = f"<h2>{html.escape(doc.doc_id)}</h2>"
                try:
                    result = extractor.process(doc.text, doc.doc_id)
                except Exception as exc:  # one bad note must not end the page
                    errors += 1
                    sections.append(
                        f"<section>{heading}<p class='error'>Could not be processed: "
                        f"{type(exc).__name__}</p></section>"
                    )
                    continue
                mentions = [
                    m for m in result.mentions if not groups or m.semantic_group.upper() in groups
                ]
                counts = {s: sum(mention_status(m) == s for m in mentions) for s in STATUSES}
                meta = ", ".join(f"{v} {LABELS[k]}" for k, v in counts.items() if v)
                sections.append(
                    f"<section>{heading}<p class='meta'>{len(mentions)} mention(s)"
                    f"{': ' + meta if meta else ''}</p>"
                    f"<div class='note'>{render_text(doc.text, mentions)}</div>"
                    f"<details><summary>Mentions table</summary>{render_table(mentions)}"
                    "</details></section>"
                )
        except (OSError, ValueError, ImportError, NotImplementedError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except KeyError as exc:  # a .jsonl record without "text"
            print(f"error: missing field {exc} in an input record", file=sys.stderr)
            return 2

    if not sections:
        print("no notes found", file=sys.stderr)
        return 1
    page = render_page(sections, mode=config.mode.value, n_notes=len(sections))
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(page, encoding="utf-8")
    except OSError as exc:
        print(f"error: could not write {args.output}: {exc}", file=sys.stderr)
        return 2
    print(f"-> {args.output} ({len(sections)} notes, {errors} failed)", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
