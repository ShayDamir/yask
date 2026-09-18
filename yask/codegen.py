"""Generate ``yask/web/js/constants.js`` from the Python source constants.

Python is the single source of truth for the cross-language constants:
the workflow states and ``DEFAULT_STATE`` (``yask.db``), the label-color
hex regex, the shared inline-markdown regex subset, and the free-text
field length limits (``yask.spec``).
This script aggregates them and emits ``yask/web/js/constants.js`` as an
ES module whose every export is a JSON literal, so
``tests/test_codegen.py`` can parse the file back and assert
value-equality against the Python constants.

Run from the repo root:

    nix develop -c python -m yask.codegen

The emitted file is committed to git. The round-trip test in
``tests/test_codegen.py`` fails when the committed file drifts from what
the Python constants generate, which forces a regeneration after any
change to the source constants.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import db, spec

OUTPUT = Path(__file__).parent / "web" / "js" / "constants.js"

HEADER = """\
// GENERATED FILE — do not edit by hand.
//
// Single-sourced from the Python constants (yask/db.py, yask/spec.py);
// the generator is yask/codegen.py. Regenerate with:
//
//     nix develop -c python -m yask.codegen
//
// Every export below is a JSON literal so tests/test_codegen.py can parse
// this file back and assert value-equality against the Python source.
"""


def generate() -> str:
    """Render the full content of constants.js (pure; no I/O)."""
    exports = {
        "WORKFLOW_STATES": db.WORKFLOW_STATES,
        "DEFAULT_STATE": db.DEFAULT_STATE,
        "BLOCKED_STATE": db.BLOCKED_STATE,
        "ARCHIVED_STATE": db.ARCHIVED_STATE,
        "ALL_STATES": db.ALL_STATES,
        "IN_PROGRESS_STATES": db.IN_PROGRESS_STATES,
        "COLOR_HEX_RE": spec.COLOR_HEX_RE,
        "MARKDOWN_REGEXES": spec.MARKDOWN,
        "FIELD_LIMITS": spec.FIELD_LIMITS,
        "MAX_IMAGE_PIXELS": spec.MAX_IMAGE_PIXELS,
        "RASTER_IMAGE_TYPES": spec.RASTER_IMAGE_TYPES,
    }
    lines = [HEADER.rstrip("\n"), ""]
    for name, value in exports.items():
        lines.append(f"export const {name} = {json.dumps(value)};")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    OUTPUT.write_text(generate(), encoding="utf-8")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
