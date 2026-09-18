"""Cross-language spec constants shared with the web UI via codegen.

These values are defined here exactly once and consumed from both
languages:

* Python imports them directly — ``store`` compiles
  :data:`COLOR_HEX_RE` (``_normalize_color``) and enforces
  :data:`FIELD_LIMITS` (every free-text field length limit) and
  :data:`MAX_IMAGE_PIXELS` (the bitmap pixel-area cap, task #127, which the
  web viewer re-checks per :data:`RASTER_IMAGE_TYPES` — task #134);
  ``telegram_bot`` compiles the :data:`MARKDOWN` regexes (the HTML-fallback
  converter's inline subset).
* JavaScript receives them through the generated
  ``yask/web/js/constants.js`` (see ``yask.codegen``); the round-trip and
  mirror tests in ``tests/test_codegen.py`` fail whenever the generated
  file drifts from this source, so the two languages cannot silently
  diverge.

Notes:

* :data:`COLOR_HEX_RE` requires a leading ``#``. The web renderer used to
  accept an optional ``#`` (``/^#?…/``), but every color that reaches the
  browser is already server-normalized to ``#RRGGBB`` by
  ``store._normalize_color`` (which enforces this very regex), so the
  ``#?`` branch was dead code; dropping it has no observable effect.
* :data:`MARKDOWN` is the **inline** subset only. The JS renderer also
  renders block-level constructs (h1–h6, ul, ol, p, …), but Telegram's
  HTML parse mode does not support those tags, so the block regexes stay
  language-local and are deliberately NOT single-sourced.
"""

# A hex color with a mandatory ``#`` prefix: ``#RGB`` or ``#RRGGBB``.
COLOR_HEX_RE = r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"

# Maximum lengths (in characters) for every free-text field. Python enforces
# these centrally in the store: a value beyond its limit raises
# ``ValidationError`` (HTTP 400 on REST, an ``{"ok": false}`` error on MCP,
# a domain message in the bot). The web forms mirror the same values as
# ``maxlength`` so the browser clips input before submit; the store check is
# the real guard. (Task #126: no field may be unbounded — oversized values
# bloat the DB and, for attachment filenames, the ``Content-Disposition``
# header of every download.)
FIELD_LIMITS = {
    "title": 255,
    "description": 65536,
    "projectName": 255,
    "taskTypeName": 255,
    "labelName": 64,
    "roleName": 64,
    "attachmentFilename": 255,
    # Telegram allowlist password (task #133): scrypt's CPU cost scales with
    # password length, so a multi-MB password from the (loopback-only,
    # unauthenticated) set endpoints is a CPU DoS — cap it well above any
    # sane password.
    "telegramPassword": 256,
}

# The bitmap pixel-area cap (task #127): a few-KB PNG/JPEG may declare a
# 30000x30000 canvas that decodes to ~3.6 GB of pixels, and the web viewer's
# ``<img>`` then freezes / OOMs the tab of whoever opens it (CWE-400).
# 25 Mpixel ≈ 100 MB of RGBA stays within what browsers decode sanely.
# Python enforces it at upload (``store.add_attachment``); the web viewer
# re-checks it when displaying bitmaps stored before that fix existed
# (task #134). Single-sourced here so the two checks cannot drift.
MAX_IMAGE_PIXELS = 25_000_000

# The bitmap content types subject to the pixel cap, sorted so the generated
# JS literal is deterministic. SVG is excluded: a vector format with no
# bitmap dimensions (and served as a forced download under an inert CSP,
# never decoded in-page).
RASTER_IMAGE_TYPES = [
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
]

# The shared inline-markdown subset, keyed by construction. The ``_star``
# suffix distinguishes the ``**``/``*`` variants from the Telegram-only
# ``__``/``_`` variants (``_STRONG_UNDER_RE``/``_EM_UNDER_RE`` in
# telegram_bot.py), which are a superset over the web renderer and stay
# language-local.
MARKDOWN = {
    "code_span": r"`([^`]+)`",
    "strong_star": r"\*\*([^*]+)\*\*",
    "em_star": r"(^|[^*])\*([^*\n]+)\*",
    "strike": r"~~([^~]+)~~",
    "link": r"\[([^\]]+)\]\((https?:[^)\s]+)\)",
}
