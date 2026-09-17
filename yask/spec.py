"""Cross-language spec constants shared with the web UI via codegen.

These values are defined here exactly once and consumed from both
languages:

* Python imports them directly — ``store`` compiles
  :data:`COLOR_HEX_RE` (``_normalize_color``) and enforces
  :data:`FIELD_LIMITS` (every free-text field length limit),
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
}

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
