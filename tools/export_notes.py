"""Write each note of a cuiflow reference JSONL back out as a text file, byte for byte.

    python tools/export_notes.py <gold.jsonl> --out notes/

The gold files store every note verbatim (offsets index ``text``), so they are the notes' source
of record: the gold set holds the 30 synthetic notes doc_01-30. ``cuiflow gold convert`` and the
integration tests' Phase 1 notes read files; this recreates them. Files are named by
``doc_id`` and never overwritten.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def export_notes(reference: Path, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = []
    with reference.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            name = rec["doc_id"]
            if Path(name).name != name or name in ("", ".", ".."):
                raise ValueError(f"doc_id {name!r} is not a plain file name")
            path = out / name
            with path.open("x", encoding="utf-8", newline="") as note:  # keep \r\n as stored
                note.write(rec["text"])
            written.append(path)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("reference", type=Path, help="cuiflow reference JSONL")
    ap.add_argument("--out", type=Path, required=True, help="folder to write the notes into")
    args = ap.parse_args()
    try:
        written = export_notes(args.reference, args.out)
    except (FileExistsError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{len(written)} notes written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
