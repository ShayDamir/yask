"""Codegen round-trip and cross-language mirror tests (task #122).

Python is the single source of truth for the cross-language constants
(workflow states + ``DEFAULT_STATE`` from ``yask.db``, the label-color
hex regex, the shared inline-markdown regex subset, the free-text field
length limits, and the bitmap pixel-cap constants from ``yask.spec``). ``yask.codegen`` emits ``yask/web/js/constants.js`` from
them; these tests pin the generated file to the Python constants so that
editing one side without regenerating (or letting a regex drift between
the two languages) fails here. There is no JS runtime in the dev
environment, so the seams are verified from Python: the round-trip diff
test plus value-equality on every JSON-literal export.
"""

import json
import re
from pathlib import Path

from yask import codegen, db, spec, store
from yask import telegram_bot

CONSTANTS_JS = Path(__file__).parent.parent / "yask" / "web" / "js" / "constants.js"

# constants.js's export grammar: one export per line, value a JSON literal.
EXPORT_RE = re.compile(r"^export const (\w+) = (.+);$")


def _exports() -> dict:
    """Parse constants.js's JSON-literal exports into a dict."""
    out = {}
    for line in CONSTANTS_JS.read_text(encoding="utf-8").splitlines():
        m = EXPORT_RE.match(line)
        if m is None:
            continue
        out[m.group(1)] = json.loads(m.group(2))
    return out


def test_codegen_roundtrip():
    """Regenerating from the Python constants must be byte-identical."""
    committed = CONSTANTS_JS.read_text(encoding="utf-8")
    assert codegen.generate() == committed


def test_states_constants():
    exp = _exports()
    assert exp["WORKFLOW_STATES"] == db.WORKFLOW_STATES
    assert exp["DEFAULT_STATE"] == db.DEFAULT_STATE
    assert exp["BLOCKED_STATE"] == db.BLOCKED_STATE
    assert exp["ARCHIVED_STATE"] == db.ARCHIVED_STATE
    assert exp["ALL_STATES"] == db.ALL_STATES
    assert exp["IN_PROGRESS_STATES"] == db.IN_PROGRESS_STATES


def test_field_limits_constants():
    """The generated FIELD_LIMITS export must equal the spec source (task
    #126): the web maxlength mirrors and the store's enforcement point
    cannot drift apart."""
    exp = _exports()
    assert exp["FIELD_LIMITS"] == spec.FIELD_LIMITS
    # every limit is a positive character count
    assert all(isinstance(v, int) and v > 0 for v in exp["FIELD_LIMITS"].values())


def test_image_pixel_constants():
    """The pixel-bomb cap and its raster types are single-sourced in spec
    (#127 store enforcement, #134 viewer re-check): the generated exports,
    the spec source, and the store's class attributes must agree so the
    upload check and the display check cannot drift in either direction."""
    exp = _exports()
    assert isinstance(exp["MAX_IMAGE_PIXELS"], int)
    assert exp["MAX_IMAGE_PIXELS"] == spec.MAX_IMAGE_PIXELS > 0
    assert exp["RASTER_IMAGE_TYPES"] == spec.RASTER_IMAGE_TYPES
    assert all(t.startswith("image/") for t in exp["RASTER_IMAGE_TYPES"])
    # the store (enforcement point) mirrors the spec
    assert store.Store.MAX_IMAGE_PIXELS == spec.MAX_IMAGE_PIXELS
    assert store.Store.RASTER_IMAGE_TYPES == set(spec.RASTER_IMAGE_TYPES)


def test_color_hex_regex_constant():
    exp = _exports()
    assert exp["COLOR_HEX_RE"] == spec.COLOR_HEX_RE
    pat = re.compile(exp["COLOR_HEX_RE"])
    assert pat.match("#ff0000") is not None
    assert pat.match("#F00") is not None
    # the leading # is mandatory: colors are server-normalized before the
    # browser ever sees them, so the old JS "#?" branch was dead code
    assert pat.match("ff0000") is None


# A corpus exercising every shared regex: emphasis, code spans, links
# (good and bad schemes), and marker-adjacent text.
_CORPUS = [
    "**bold** and *italic* and ~~strike~~",
    "use `code` and a_b_c identifiers",
    "[link](https://e.com) and [bad](ftp://x) and [u](http://y.com)",
    "mixed **`code`** `a`b` ~~*s*~~ *a* b *c d**e**",
    "**`**` and [x](https://a.com) [y](https://b.com)",
]


def test_markdown_regex_constants():
    exp = _exports()
    # The compiled module regexes must match the spec source exactly.
    compiled = {
        "code_span": telegram_bot._CODE_SPAN_RE,
        "strong_star": telegram_bot._STRONG_STAR_RE,
        "em_star": telegram_bot._EM_STAR_RE,
        "strike": telegram_bot._STRIKE_RE,
        "link": telegram_bot._LINK_RE,
    }
    assert set(exp["MARKDOWN_REGEXES"]) == set(spec.MARKDOWN)
    for key, rx in compiled.items():
        assert spec.MARKDOWN[key] == rx.pattern
        assert exp["MARKDOWN_REGEXES"][key] == rx.pattern
    # The generated strings must compile and behave like the Python
    # ones on a corpus (findall equivalence per regex and per input).
    for key, rx in compiled.items():
        js_rx = re.compile(exp["MARKDOWN_REGEXES"][key])  # also proves valid
        for text in _CORPUS:
            assert js_rx.findall(text) == rx.findall(text)


def test_markdown_regexes_cover_the_inline_pipeline():
    """Every shared regex the JS renderer applies must exist in the spec."""
    # The JS inline() pipeline (markdown.js) applies exactly these five,
    # in this order — if a new inline rule is added to markdown.js it
    # belongs in spec.MARKDOWN and this set grows with it.
    assert set(spec.MARKDOWN) == {
        "code_span",
        "strong_star",
        "em_star",
        "strike",
        "link",
    }
