"""Telegram bot process for yask.

Run via ``yask telegram``: validates the bot token (``TELEGRAM_BOT_TOKEN``)
with ``getMe``, opens the yask store, then long-polls the Bot API with
``getUpdates`` (subscribing to messages and inline-keyboard callbacks) and
answers incoming messages. Board access is password-authenticated: the
permitted chats (a chat id plus a password, stored only as a salted hash in
the store's ``telegram_users`` table and managed from the web UI) may
``/login <password>``; a successful login stamps a session in the store
that persists across bot restarts (a password rotation or a removal from
the web UI revokes it, requiring a fresh ``/login``); until a chat has
authenticated, the board commands and every inline-keyboard callback
answer with an auth-required notice and no board data, and state-change
notifications are not delivered to it. The ungated
commands are ``/start``, ``/help``, ``/login`` and ``/whoami`` (the chat's
own id — the identifier the administrator enters in the web UI). Today the
authenticated bot answers
``/projects`` (the project list with per-state task
counts, with one inline button per project), ``/tasks`` (the tasks in the
active states — Todo, Planning, In progress and Review — grouped by
project and state, with one inline button per task),
``/task <project> <number|title>`` (one task's details — state, estimate,
description, prerequisites, attachments and recent history — the task
found by number or by case-insensitive title),
``/backlog [project]`` (the tasks in the Backlog state, grouped by
project, with one inline button per task), ``/attachment
 <project> <task> <id>`` (shows one of the task's attachments — small
markdown (<16 KB) inline as a Rich Message, small plain text inline as a
plain message, images as a photo, larger content as a file, via
``sendRichMessage``/``sendDocument``/``sendPhoto``),
``/move <project> <task> <state>`` (moves a task to another workflow
state — a move that would pull prerequisites along is confirmed with
inline buttons first, nothing is applied before the confirmation) and
 ``/add <project> <title> [as <type>]`` (creates a new task of the given
  task type — default ``Task`` — in the project's backlog; ``<type>`` is
  one of the board's task types, so custom types can be created too; the
  confirmation reply carries an inline button opening the new task's detail
  view, plus a Main-menu row),
 ``/describe <project> <number|title> <description>`` (sets — replaces —
  the task's description; the number form resolves strictly by number, the
  title form by longest unique title prefix, so the remaining words become
  the description),
 ``/type <project> <number|title> <type>`` (changes the task's type —
  one of the board's task types, matched case-insensitively — using the
  same store write the web UI and MCP use; the number form resolves
  strictly by number, the title form by longest unique title prefix, so
  the remaining words name the type) and
 ``/attach <project> <number|title>`` (attach a file to a task: send a
  document or a photo to the bot whose caption is
  ``/attach <project> <number|title>`` — the bot downloads the file from
  the Bot API and stores it on the task; markdown/plain text and
  png/jpeg/gif/webp/svg images up to 10 MB are accepted, other types and
  larger files are refused before anything is downloaded; a missing
  caption, a caption of another command, or a resolution failure answers a
  text and uploads nothing; typing ``/attach`` as a plain text message
  answers the usage text); all
board reads and writes go through the store. The file handler is the only
async part of the message dispatch: a file message's reply is a coroutine
that ``run_bot`` awaits, every other handler stays synchronous. A chat can
``/subscribe
<project>`` to receive
task state-change notifications for that project (``/unsubscribe
<project>`` to stop; subscriptions are per chat, per project, and persist
in the same SQLite database). Inline-keyboard callbacks
(``callback_query`` updates) are answered through a second dispatch layer,
:func:`make_callback_dispatch`: every callback is answered (the client's
progress bar hangs until it is answered) and an unrecognized payload gets
a toast instead of a crash; the ``p:`` payload (the per-project buttons of
``/projects``) opens the project's task list view as a new message, the
``t:`` payload (the per-task buttons of ``/tasks``) opens the task's detail
view as a new message (the detail's keyboard also carries one state
button per other workflow state), the ``a:`` payload (the per-attachment
buttons of the ``/task`` view) shows the attachment in the chat — small
markdown (<16 KB) inline as a Rich Message, small plain text inline as a
plain message, images as a photo, larger content as a file — the
``s:``/``u:`` payload (the
subscribe/unsubscribe toggle of the ``/task`` view) toggles the chat's
subscription and flips the button in place, and the ``m:``/``c:``/``x:``
payload (the per-state buttons of the ``/task`` view) moves the task to a
workflow state in place — a move that would pull prerequisites along is
confirmed first (``c:`` confirms the cascade, ``x:`` cancels). The ``h:``
payload (the main-menu hub's buttons) opens the menu view itself (``h``)
or one of its routes — the project list (``h:p``), the all-projects task
list (``h:t``), the chat's subscription list (``h:s``), the add-task usage
(``h:a``) and the help text (``h:h``); every board view's keyboard
(``/projects``, ``/tasks``, ``/task`` and the notifications) carries a
trailing ``Main menu`` row with the bare ``h`` payload, so the hub is one
tap away from anywhere in the chat, and is the natural entry point:
``/start`` replies with the intro text (carrying the ``/login`` guidance)
plus the hub's keyboard, and ``/help`` with the command reference plus a
Main-menu button. While the
bot runs, the
:class:`Notifier` polls ``state_history`` after each successful
``getUpdates`` batch and pushes a message per transition to every
subscribed chat, each carrying one inline button (payload
``t:<project-id>:<number>``) that opens the task's detail view and a
Main-menu row (payload ``h``); latency is
at most one poll interval. Later features of the Telegram interface (Epic #27)
extend the dispatch layer on top of the store passed in here.

The bot talks to the Bot API directly with ``httpx`` (already a project
dependency). The client accepts an injected ``httpx.AsyncClient`` so tests
can mock the Bot API at the HTTP layer with ``httpx.MockTransport`` — tests
never call real Telegram.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import re
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

import httpx

from . import db
from .store import NotFound, Store, ValidationError

# Bot API base URL. Overridable for tests/future (no env override needed now).
DEFAULT_BASE_URL = "https://api.telegram.org"
# Long-poll timeout (seconds) passed to getUpdates.
POLL_TIMEOUT = 30
# httpx timeout (seconds) for the default client; must exceed the long-poll
# timeout so a poll blocking server-side is not cut off client-side.
HTTP_TIMEOUT = 65.0
# Cap on the 429 retry_after back-off (seconds).
MAX_RETRY_AFTER = 30.0

START_TEXT = (
    "Hello! I am the yask Telegram bot.\n\n"
    "I am connected to a yask kanban board and answer commands about it.\n\n"
    "Board commands require authentication: /login <password> first (your\n"
    "yask administrator permits your chat in the web UI; /whoami shows\n"
    "your chat id).\n\n"
    "Use /help to see what I can do."
)

# The single command registry: the source of truth for what this bot offers.
# A command's name, one-line description and auth-gate flag live in exactly
# one place. HELP_TEXT (:func:`render_help`), the command menu payload
# (setMyCommands, #65) and the dispatch table (:data:`COMMAND_TABLE`,
# consumed by make_dispatch) all derive from it: the dispatch table is
# verified against the registry's auth-gated set at import
# (:func:`_verify_dispatch_table`), so help, menu and dispatch can never
# diverge.

@dataclass(frozen=True)
class Command:
    """One bot command: its name, a one-line description and whether board
    access (a /login session) is required to use it."""

    name: str
    description: str
    auth_gated: bool

    def __post_init__(self) -> None:
        # Telegram caps a command at 32 chars and a description at 256;
        # enforce both at construction so the source of truth can never
        # exceed an API limit.
        if len(self.name) > 32:
            raise ValueError(
                f"command name too long ({len(self.name)} > 32): {self.name!r}"
            )
        if len(self.description) > 256:
            raise ValueError(
                f"description too long ({len(self.description)} > 256): "
                f"{self.description!r}"
            )


COMMAND_REGISTRY: list[Command] = [
    Command("/start", "main menu (intro + login guidance)", auth_gated=False),
    Command("/help", "this help", auth_gated=False),
    Command(
        "/login",
        "authenticate this chat with its password (required for board commands)",
        auth_gated=False,
    ),
    Command(
        "/whoami",
        "show this chat's id (give it to the yask administrator)",
        auth_gated=False,
    ),
    Command("/projects", "list of projects with per-state task counts", auth_gated=True),
    Command(
        "/tasks",
        "tasks in Todo, Planning, In progress and Review (optionally [project])",
        auth_gated=True,
    ),
    Command(
        "/task",
        "details of one task (/task <project> <number|title>)",
        auth_gated=True,
    ),
    Command(
        "/backlog",
        "tasks in Backlog (optionally [project])",
        auth_gated=True,
    ),
    Command(
        "/move",
        "move a task to another state (/move <project> <task> <state>)",
        auth_gated=True,
    ),
    Command(
        "/add",
        "add a task to the project's backlog "
        "(/add <project> <title> [as <type>])",
        auth_gated=True,
    ),
    Command(
        "/describe",
        "set a task's description "
        "(/describe <project> <number|title> <description>)",
        auth_gated=True,
    ),
    Command(
        "/type",
        "change a task's type "
        "(/type <project> <number|title> <type>)",
        auth_gated=True,
    ),
    Command(
        "/attachment",
        "show a task's attachment (/attachment <project> <task> <id>)",
        auth_gated=True,
    ),
    Command(
        "/attach",
        "attach a file to a task (send a document or photo captioned "
        "/attach <project> <number|title>)",
        auth_gated=True,
    ),
    Command(
        "/subscribe",
        "subscribe to task state-change notifications (optionally [project])",
        auth_gated=True,
    ),
    Command(
        "/unsubscribe",
        "stop notifications for a project (optionally [project])",
        auth_gated=True,
    ),
]

# setMyCommands scope: the bot is a private-chat, password-gated bot, so the
# command menu applies to all of this bot's private chats. See #66 decision log.
MENU_SCOPE = {"type": "all_private_chats"}

HELP_FOOTER = (
    "I read the yask board that this process was started with\n"
    "(yask telegram --data DIR). More commands are on the way."
)


def render_help(commands: list[Command]) -> str:
    """Build the ``/help`` reference from a command list (``COMMAND_REGISTRY``).

    One ``<name> — <description>`` line per command, in registry order, under a
    ``Commands:`` header and :data:`HELP_FOOTER`. Every command is listed
    (auth-gated ones included) so the help always covers the full set; the
    text is generated, not hand-written, so help can never omit or misspell a
    command that lives in the registry. ``commands`` is a parameter (not a
    closure over ``COMMAND_REGISTRY``) so the formatter is unit-testable.
    """
    lines = ["Commands:"]
    for command in commands:
        lines.append(f"{command.name} — {command.description}")
    lines.append("")
    lines.append(HELP_FOOTER)
    return "\n".join(lines)


HELP_TEXT = render_help(COMMAND_REGISTRY)


def build_my_commands(commands: list[Command]) -> list[dict]:
    """One ``{command, description}`` entry per command, in registry order.

    The source of truth for the command menu (setMyCommands, #65), so the menu
    and ``/help`` can never diverge. Every command is listed (auth-gated ones
    included — the menu is presentation-only; the bot still gates them).
    ``commands`` is a parameter (not a closure over ``COMMAND_REGISTRY``) so
    the builder is unit-testable.
    """
    return [
        {"command": c.name, "description": c.description} for c in commands
    ]


# Board access is gated: unauthenticated chats get this instead of any
# board data (commands and inline-keyboard callbacks alike).
AUTH_REQUIRED_TEXT = (
    "This command requires authentication. Use /login <password>."
)

LOGIN_USAGE_TEXT = "Usage: /login <password>"

LOGIN_OK_TEXT = "Authenticated. You can now use the board."

# Unknown chat and wrong password are indistinguishable on purpose: the
# bot must not reveal which chat ids exist in the allowlist.
LOGIN_FAIL_TEXT = "Authentication failed. Check your password and try again."

WHOAMI_TEXT = (
    "Your chat id is {chat_id}. "
    "Give it to your yask administrator to get access."
)

UNKNOWN_HINT = "I don't understand that. Try /help to see what I can do."

# A callback_query no payload handler recognizes (a stale button from a
# deleted task or an older bot version) gets this toast — the bot always
# answers every callback, so the client's progress bar never hangs.
UNKNOWN_CALLBACK_TEXT = (
    "This button is out of date. Try /help to see what I can do."
)

# Store-backed command failed: reply, don't crash the poll loop.
PROJECTS_ERROR_TEXT = "I could not read the board right now. Please try again."
TASKS_ERROR_TEXT = "I could not read the board right now. Please try again."
BACKLOG_ERROR_TEXT = "I could not read the board right now. Please try again."
TASK_ERROR_TEXT = "I could not read the board right now. Please try again."
ATTACHMENT_ERROR_TEXT = "I could not read the board right now. Please try again."
SUBSCRIBE_ERROR_TEXT = (
    "I could not change your subscription right now. Please try again."
)
UNSUBSCRIBE_ERROR_TEXT = (
    "I could not change your subscription right now. Please try again."
)
MOVE_ERROR_TEXT = "I could not write to the board right now. Please try again."
ADD_ERROR_TEXT = "I could not write to the board right now. Please try again."
DESCRIBE_ERROR_TEXT = (
    "I could not write to the board right now. Please try again."
)
TYPE_ERROR_TEXT = (
    "I could not write to the board right now. Please try again."
)
ATTACH_ERROR_TEXT = (
    "I could not write to the board right now. Please try again."
)

TASK_USAGE_TEXT = (
    "Usage: /task <project> <number|title>\n"
    "Shows one task's details: state, estimate, description, prerequisites,\n"
    "attachments and recent history.\n"
    "Example: /task yask 4 or /task yask fix the bug"
)

ATTACHMENT_USAGE_TEXT = (
    "Usage: /attachment <project> <task> <attachment-id>\n"
    "Shows one of the task's attachments: small markdown/plain text (<16 KB)\n"
    "inline in the chat, images as a photo, larger content as a file.\n"
    "List a task's attachments with /task <project> <number|title>."
)

MOVE_USAGE_TEXT = (
    "Usage: /move <project> <task> <state>\n"
    "Moves a task to another state: Backlog, Todo, Planning, In progress,\n"
    "Review or Done.\n"
    "Example: /move yask 4 In progress"
)

DESCRIBE_USAGE_TEXT = (
    "Usage: /describe <project> <number|title> <description>\n"
    "Sets the task's description (replacing any existing one).\n"
    "Example: /describe yask 4 Fix the login bug"
)

ATTACH_USAGE_TEXT = (
    "Usage: /attach <project> <number|title>\n"
    "Attach a file to a task: send a document or a photo to this chat\n"
    "with a caption of /attach <project> <number|title>.\n"
    "Allowed: markdown/plain text and png/jpeg/gif/webp/svg images,\n"
    "10 MB max.\n"
    "Example caption: /attach yask 4"
)

def add_usage_text(store: Store) -> str:
    """The ``/add`` usage, listing the board's current task types.

    The type list is read from the board (``store.list_task_types``), not a
    hardcode, so custom types (e.g. ``Investigation``) are discoverable.
    """
    names = [t["name"] for t in store.list_task_types()]
    return (
        "Usage: /add <project> <title> [as <type>]\n"
        "Creates a new task in the project's backlog; type is one of: "
        + ", ".join(names)
        + " (default: Task).\n"
        "Example: /add yask fix the login bug as Bug"
    )


def type_usage_text(store: Store) -> str:
    """The ``/type`` usage, listing the board's current task types.

    The type list is read from the board (``store.list_task_types``), not a
    hardcode, so custom types (e.g. ``Investigation``) are discoverable —
    the same convention as :func:`add_usage_text`.
    """
    names = [t["name"] for t in store.list_task_types()]
    return (
        "Usage: /type <project> <number|title> <type>\n"
        "Changes the task's type; type is one of: "
        + ", ".join(names)
        + ".\n"
        "Example: /type yask 4 Bug"
    )

# The rich task view lives under the Rich Message budget
# (:data:`RICH_MESSAGE_MAX`, 32 768 chars): the description is capped
# below it, leaving headroom for the header, the section lists and the
# truncation note. A pathological overflow (dozens of attachments) 400s
# the rich leg and degrades through the fallback chain, whose
# HTML/plain legs re-truncate at send time.
DESCRIPTION_MAX = 30000
HISTORY_MAX = 10

# Attachments strictly smaller than this are rendered inline instead of
# being sent as a downloadable document (the story's "<16KB" threshold).
INLINE_MARKDOWN_MAX_SIZE = 16 * 1024
# Char budget for an inline attachment message. Telegram caps a message at
# 4096 (measured in UTF-16 code units); 4000 leaves room for the
# truncation note and non-BMP characters.
INLINE_TEXT_MAX = 4000

# Rich Message cap (Bot API 10.1's sendRichMessage), measured in UTF-8
# characters — shared by the epic's rich reply formatters, which apply it.
RICH_MESSAGE_MAX = 32768
# Code-point budget for the rich fallback's HTML/plain legs. Telegram caps
# a regular message at 4096 (measured in UTF-16 code units); 4000 code
# points leaves room for the truncation note and non-BMP characters (each
# counting as two UTF-16 units), the same convention INLINE_TEXT_MAX
# documents.
REGULAR_TEXT_MAX = 4000

# Display cap for the state-change notification's button label. Telegram
# documents no limit on inline button text (only ``callback_data`` is
# hard-capped at 64 bytes, which ``t:<project-id>:<number>`` always
# satisfies), so 64 chars is a compact, safe display choice.
NOTIFICATION_BUTTON_TEXT_MAX = 64

class BotAPIError(Exception):
    """Bot API failure: an ``ok:false`` response or a transport failure.

    Carries the API ``error_code`` (when present) and a human-readable
    ``description``.

    Messages are guaranteed token-free: transport failures are redacted at
    the ``httpx`` error wrap sites in :class:`BotAPI`.
    """

    def __init__(self, description: str, error_code: Optional[int] = None) -> None:
        super().__init__(description)
        self.error_code = error_code
        self.description = description

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.description


class Auth:
    """The chats that currently hold a login session, as the store sees them.

    The allowlist (who *may* authenticate, with which password) and the
    sessions themselves both live in the store's ``telegram_users`` table —
    the allowlist is managed from the web UI, stored only as salted hashes.
    A successful ``/login`` stamps the session in the database, so it
    survives bot restarts. A password rotation or a removal from the web UI
    invalidates the session (NULL / row gone), and because the store is
    consulted on every check, the revocation bites immediately — even in a
    running bot process — no restart needed.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def is_authenticated(self, chat_id: Optional[int]) -> bool:
        """Whether this chat has a persisted login session."""
        if chat_id is None:
            return False
        return self._store.is_telegram_user_authenticated(chat_id)

    def authenticate(self, chat_id: Optional[int], password: str) -> bool:
        """Check the chat's password against the store's allowlist.

        On success the chat's login session is stamped in the store — it
        survives bot restarts. A ``None`` chat (no sender) and an unknown
        chat or wrong password all return ``False`` — the bot cannot tell
        the cases apart.
        """
        if chat_id is None:
            return False
        return self._store.login_telegram_user(chat_id, password)


def _command_token(text: Optional[str]) -> Optional[str]:
    """Lowercased leading command of ``text`` (``/cmd@bot`` → ``/cmd``).

    Returns None when the message carries no command to dispatch: non-text,
    blank, or a first word that is not a ``/command``.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    token = text.strip().split()[0]
    if not token.startswith("/"):
        return None
    return token.split("@", 1)[0].lower()


def reply_for(text: Optional[str]) -> Optional[Reply]:
    """Reply for an incoming message, or None if there is nothing to say.

    Non-text messages (stickers, photos, ...) get no reply; unknown input
    gets a short "try /help" hint. A ``/command@botname`` suffix is ignored.
    The table's values are :class:`KeyboardReply` — ``/start`` the intro
    plus the hub's keyboard (:func:`start_view`), ``/help`` the command
    reference plus a Main-menu button (:func:`help_view`) — unknown chatter
    stays a plain hint string.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    cmd = _command_token(text)
    if cmd is None:
        return UNKNOWN_HINT
    return COMMANDS.get(cmd, UNKNOWN_HINT)


def project_view(store: Store) -> Reply:
    """Format the ``/projects`` reply.

    One line per project in name order, prefixed with the project's DB id
    (the id the web API and MCP tools use); each line lists the task counts
    of the states that have tasks, in canonical state order. A project with
    no visible tasks is listed without a state segment.

    A non-empty board is a :class:`KeyboardReply`: the same projects, in
    reading order, become one inline-keyboard row each with a single
    button — label = the project name, ``callback_data`` =
    ``p:<project-id>`` (the project drill-down button, answered by
    :func:`make_callback_dispatch`) — and a final row carrying the
    Main-menu button (:func:`_main_menu_button`, payload ``h``). An empty
    board is just ``Projects:``
    and ``(none)`` as a plain ``str`` — the Bot API rejects an empty
    inline keyboard, and there are no tap targets anyway.
    """
    overviews = store.list_project_overviews()
    if not overviews:
        return "Projects:\n(none)"
    lines = ["Projects:"]
    rows = []
    for ov in overviews:
        line = f"{ov['id']}. {ov['name']}"
        if ov["states"]:
            line += " — " + ", ".join(
                f"{state}: {n}" for state, n in ov["states"].items()
            )
        lines.append(line)
        rows.append(
            [{"text": ov["name"], "callback_data": f"p:{ov['id']}"}]
        )
    rows.append([_main_menu_button()])
    return KeyboardReply("\n".join(lines), {"inline_keyboard": rows})


def _arg_words(text: Optional[str]) -> Optional[list[str]]:
    """Argument words for a command: everything after the first word.

    The words following the leading command token (any ``@botname`` mention
    is part of that token and discarded), or ``None`` when the message has
    no arguments: not a string, blank, or just the command itself.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    words = text.strip().split()
    if len(words) < 2:
        return None
    return words[1:]


def _tasks_arg(text: Optional[str]) -> Optional[str]:
    """Argument words for ``/tasks``: everything after the command token.

    Joins the words with a single space so project names containing spaces
    work; returns ``None`` when the message is just the command. The command
    token (and any ``@botname`` mention) is discarded.
    """
    words = _arg_words(text)
    if words is None:
        return None
    return " ".join(words)


def _split_project(
    store: Store, words: list[str]
) -> tuple[Optional[dict], list[str]]:
    """Split argument words into a project reference and the rest.

    Walks the words longest-prefix first and resolves each prefix as a
    project (case-insensitive name, then integer id — the same rules as
    ``/tasks``); the longest prefix that resolves wins, and everything after
    it is the remaining argument words. When no prefix resolves, the first
    word is kept as the failed reference so the not-found reply can quote
    it: ``(None, [words[0]])``.
    """
    for i in range(len(words), 0, -1):
        project = _resolve_project(store, " ".join(words[:i]))
        if project is not None:
            return project, words[i:]
    return None, [words[0]]


def _human_size(n: int) -> str:
    """A byte count in B/KB/MB (one decimal from KB up)."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _resolve_project(store: Store, arg: str) -> Optional[dict]:
    """Resolve a project reference: case-insensitive name match, then id."""
    arg = arg.strip()
    projects = store.list_projects()
    for p in projects:
        if p["name"].lower() == arg.lower():
            return p
    for p in projects:
        if str(p["id"]) == arg:
            return p
    return None


def _task_sections(tasks: list[dict]) -> list[tuple[str, list[dict]]]:
    """The non-empty state groups of one project's active tasks.

    ``(state, tasks)`` pairs in canonical state order (:data:`db.
    IN_PROGRESS_STATES`, empty states skipped); within a state the tasks
    keep their ``sort_order`` (the ``list_in_progress`` order). The single
    ordered structure the ``/tasks`` text lines and the inline-keyboard
    rows are both derived from, so button order can never diverge from
    reading order.
    """
    sections = []
    for state in db.IN_PROGRESS_STATES:
        in_state = [t for t in tasks if t["state"] == state]
        if in_state:
            sections.append((state, in_state))
    return sections


def tasks_view(store: Store, project_arg: Optional[str] = None) -> Reply:
    """Format the ``/tasks [project]`` reply.

    Without an argument, lists every project (in name order, the same order
    as ``/projects``) that has at least one task in an active state, grouped
    by state. With an argument, resolves the project (case-insensitive name
    or integer id) and lists only its active tasks. An empty board — or a
    resolved project with no active tasks — shows ``(none)`` under the
    header; an unresolvable argument yields a not-found reply pointing at
    ``/projects``.

    When at least one task is listed the reply is a
    :class:`KeyboardReply`: the task lines read ``#<n> <title>`` and the
    same tasks, in reading order, become one inline-keyboard row each with
    label ``#<n> <title>`` and ``callback_data``
    ``t:<project-id>:<number>`` (the task-detail button, answered by
    :func:`make_callback_dispatch`), followed by a final row carrying the
    Main-menu button (:func:`_main_menu_button`, payload ``h``). A reply
    with no tasks is a plain ``str`` — the Bot API rejects an empty inline
    keyboard, and there are no tap targets anyway.
    """
    if project_arg is None:
        projects = []
        for p in store.list_projects():
            tasks = store.list_in_progress(p["id"])
            if tasks:
                projects.append((p, tasks))
        if not projects:
            return "Tasks in progress:\n(none)"
    else:
        project = _resolve_project(store, project_arg)
        if project is None:
            return (
                f"Project '{project_arg}' not found. Use /projects to list projects."
            )
        tasks = store.list_in_progress(project["id"])
        if not tasks:
            return "Tasks in progress:\n(none)"
        projects = [(project, tasks)]

    lines = ["Tasks in progress:"]
    rows = []
    for p, tasks in projects:
        lines.append(f"{p['id']}. {p['name']}")
        for state, in_state in _task_sections(tasks):
            lines.append(f"  {state}:")
            for t in in_state:
                lines.append(f"    #{t['number']} {t['title']}")
                rows.append(
                    [
                        {
                            "text": f"#{t['number']} {t['title']}",
                            "callback_data": f"t:{p['id']}:{t['number']}",
                        }
                    ]
                )
    rows.append([_main_menu_button()])
    return KeyboardReply("\n".join(lines), {"inline_keyboard": rows})


def backlog_view(store: Store, project_arg: Optional[str] = None) -> Reply:
    """Format the ``/backlog [project]`` reply.

    Without an argument, lists every project (in name order, the same order
    as ``/projects``) that has at least one task in the Backlog state. With
    an argument, resolves the project (case-insensitive name or integer id)
    and lists only its Backlog tasks. An empty board — or a resolved project
    with no Backlog tasks — shows ``(none)`` under the header; an
    unresolvable argument yields a not-found reply pointing at ``/projects``.

    When at least one task is listed the reply is a
    :class:`KeyboardReply`: the task lines read ``#<n> <title>`` and the
    same tasks, in reading order, become one inline-keyboard row each with
    label ``#<n> <title>`` and ``callback_data``
    ``t:<project-id>:<number>`` (the task-detail button, answered by
    :func:`make_callback_dispatch`), followed by a final row carrying the
    Main-menu button (:func:`_main_menu_button`, payload ``h``). A reply
    with no tasks is a plain ``str`` — the Bot API rejects an empty inline
    keyboard, and there are no tap targets anyway.
    """
    if project_arg is None:
        projects = []
        for p in store.list_projects():
            tasks = store.list_backlog(p["id"])
            if tasks:
                projects.append((p, tasks))
        if not projects:
            return "Backlog:\n(none)"
    else:
        project = _resolve_project(store, project_arg)
        if project is None:
            return (
                f"Project '{project_arg}' not found. Use /projects to list projects."
            )
        tasks = store.list_backlog(project["id"])
        if not tasks:
            return "Backlog:\n(none)"
        projects = [(project, tasks)]

    lines = ["Backlog:"]
    rows = []
    for p, tasks in projects:
        lines.append(f"{p['id']}. {p['name']}")
        for t in tasks:
            lines.append(f"    #{t['number']} {t['title']}")
            rows.append(
                [
                    {
                        "text": f"#{t['number']} {t['title']}",
                        "callback_data": f"t:{p['id']}:{t['number']}",
                    }
                ]
            )
    rows.append([_main_menu_button()])
    return KeyboardReply("\n".join(lines), {"inline_keyboard": rows})


def menu_view() -> KeyboardReply:
    """Format the main-menu hub reply.

    A short text and a compact inline keyboard: a Projects and a Tasks
    button on the first row (the board's two top-level list views), then
    Subscriptions, Add task and Help on the second row. Each button's
    ``callback_data`` is the ``h:<route>`` payload (``h:p``, ``h:t``,
    ``h:s``, ``h:a``, ``h:h``), answered by
    :func:`make_callback_dispatch`; the bare ``h`` payload — the button
    the other views' Main-menu rows carry (#61) — opens this same hub.
    The reply is always a :class:`KeyboardReply`: the menu's buttons are
    the point of the view.
    """
    text = "Main menu — tap a button to open a board view."
    rows = [
        [
            {"text": "Projects", "callback_data": "h:p"},
            {"text": "Tasks", "callback_data": "h:t"},
        ],
        [
            {"text": "Subscriptions", "callback_data": "h:s"},
            {"text": "Add task", "callback_data": "h:a"},
            {"text": "Help", "callback_data": "h:h"},
        ],
    ]
    return KeyboardReply(text, {"inline_keyboard": rows})


# An inline-keyboard payload for a reply: the JSON object the Bot API takes
# (e.g. ``{"inline_keyboard": [[{"text": "..", "callback_data": "t:1:4"}]]}``);
# on file uploads the BotAPI serializes it to a JSON string (multipart form
# fields are strings).
ReplyMarkup = dict


@dataclass(frozen=True)
class FileReply:
    """A reply that is a file, not a text message.

    ``run_bot`` sends it to the chat as a photo (``image/*`` content types)
    or a document, with ``caption`` riding along and (when set) an inline
    keyboard as ``reply_markup``; the dispatch layer never touches the Bot
    API itself.
    """

    filename: str
    data: bytes
    content_type: str
    caption: str
    reply_markup: Optional[ReplyMarkup] = None

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")


@dataclass(frozen=True)
class KeyboardReply:
    """A reply that is a text message with an inline keyboard.

    ``run_bot`` sends it with ``sendMessage``, the keyboard as
    ``reply_markup`` — the tap target lands in the same message as the
    text.
    """

    text: str
    reply_markup: ReplyMarkup


@dataclass(frozen=True)
class RichReply:
    """A reply whose text is markdown, sent as a Rich Message.

    ``run_bot`` sends it with ``sendRichMessage`` (Bot API 10.1+); on a
    rich-send failure it falls back to ``sendMessage`` with
    ``parse_mode=HTML`` (the markdown via :func:`markdown_to_html`), then
    to plain text — see :func:`_send_rich_with_fallback`.
    """

    markdown: str
    reply_markup: Optional[ReplyMarkup] = None


@dataclass(frozen=True)
class MessageEdit:
    """An in-place update of the original message (``editMessageText``).

    A callback action edits the message a button lives in (e.g. the
    subscribe toggle) instead of stacking a new message; the keyboard is
    replaced when ``reply_markup`` is set. The plain counterpart of
    :class:`RichMessageEdit`: the payload when the original message is a
    plain text message.
    """

    text: str
    reply_markup: Optional[ReplyMarkup] = None


@dataclass(frozen=True)
class RichMessageEdit:
    """An in-place update of a Rich Message (``editMessageText``'s
    ``rich_message`` payload, Bot API 10.1+).

    The :class:`MessageEdit` counterpart for rich originals: a text edit
    of a rich message fails server-side, so editing one requires the rich
    payload. ``run_bot`` degrades to a fresh message through the regular
    send chain when the rich edit fails — there is no plain edit leg
    below rich.
    """

    markdown: str
    reply_markup: Optional[ReplyMarkup] = None


# Every shape a dispatch layer may return: plain text, text with a
# keyboard, a file (with an optional keyboard), or markdown rendered as a
# Rich Message (with an optional keyboard).
Reply = Union[str, KeyboardReply, FileReply, RichReply]


@dataclass(frozen=True)
class IncomingFile:
    """A document or photo arriving in a message (the ``/attach`` channel).

    ``run_bot`` builds one from each file message and passes it to
    ``dispatch`` alongside (the empty) text; the dispatch layer's async
    file handler downloads the file from the Bot API and stores it on the
    resolved task. ``filename``/``content_type`` are the file's declared
    metadata (None when Telegram omits them — the handler falls back to
    ``"attachment"`` / ``"application/octet-stream"``, which the store's
    type allowlist then rejects), ``size`` the declared size in bytes
    (None when unknown — the store's size cap still binds on the bytes).
    """

    file_id: Optional[str]
    is_photo: bool
    filename: Optional[str]
    content_type: Optional[str]
    size: Optional[int]
    caption: Optional[str]


def _extract_incoming_file(message: dict) -> Optional[IncomingFile]:
    """The document or photo carried by ``message``, or None.

    A document keeps its ``file_name``/``mime_type``/``file_size``; a photo
    is the largest :class:`PhotoSize` (the last element of ``message["photo"]``)
    with a fixed ``photo.jpg`` / ``image/jpeg`` identity — a photo is always
    a JPEG, and the largest size is the one worth attaching. ``caption`` is
    the message's caption (the ``/attach`` command channel). Other media
    (video, sticker, voice, ...) yield None: they are ignored, as before.
    """
    document = message.get("document")
    if isinstance(document, dict):
        return IncomingFile(
            file_id=document.get("file_id"),
            is_photo=False,
            filename=document.get("file_name"),
            content_type=document.get("mime_type"),
            size=document.get("file_size"),
            caption=message.get("caption"),
        )
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        largest = photos[-1]
        return IncomingFile(
            file_id=largest.get("file_id"),
            is_photo=True,
            filename="photo.jpg",
            content_type="image/jpeg",
            size=largest.get("file_size"),
            caption=message.get("caption"),
        )
    return None


@dataclass(frozen=True)
class CallbackAction:
    """The result of a callback_query dispatch.

    The dispatcher sets at most one of ``reply`` (send a new message) and
    ``edit`` (update the original message in place — a plain
    :class:`MessageEdit` or, for a rich original, a
    :class:`RichMessageEdit`); ``run_bot`` answers the callback first,
    then edits, then sends. ``answer_text`` is the toast shown under the
    button (None → answer with no text), ``show_alert`` upgrades it to an
    alert dialog, and ``cache_time`` the client-side answer cache TTL in
    seconds.
    """

    answer_text: Optional[str] = None
    show_alert: bool = False
    cache_time: Optional[int] = None
    reply: Optional[Reply] = None
    edit: Optional[Union[MessageEdit, RichMessageEdit]] = None


def _toggle_button(project_id: int, subscribed: bool) -> dict:
    """The subscribe/unsubscribe toggle button of a task view's keyboard.

    ``Subscribe`` (payload ``s:<project-id>``) when the chat is not
    subscribed to the project, ``Unsubscribe`` (payload ``u:<project-id>``)
    when it is; both are answered by :func:`make_callback_dispatch`.
    """
    if subscribed:
        return {"text": "Unsubscribe", "callback_data": f"u:{project_id}"}
    return {"text": "Subscribe", "callback_data": f"s:{project_id}"}


def _main_menu_button() -> dict:
    """The Main-menu button every board view's keyboard carries (#61).

    Label ``Main menu``, payload the bare ``h`` (the menu hub's own button,
    answered by :func:`make_callback_dispatch` → :func:`menu_view`). One
    factory so the views can never diverge.
    """
    return {"text": "Main menu", "callback_data": "h"}


def start_view() -> KeyboardReply:
    """Format the ``/start`` reply: the intro plus the main-menu hub (#62).

    The static table's ungated entry point: :data:`START_TEXT` verbatim
    (it carries the ``/login`` guidance for unauthenticated chats) with
    the hub's keyboard — taken from :func:`menu_view`, so the ``/start``
    keyboard and the hub can never diverge. The reply itself stays
    ungated; the hub's board buttons answer unauthenticated chats with
    :data:`AUTH_REQUIRED_TEXT` through the existing ``h:`` family gate.
    """
    return KeyboardReply(START_TEXT, menu_view().reply_markup)


def help_view() -> KeyboardReply:
    """Format the ``/help`` reply: the command reference plus a menu row.

    The static table's other ungated entry: :data:`HELP_TEXT` verbatim
    (the full command reference) plus one trailing Main-menu row — the
    shared :func:`_main_menu_button` factory, the same row the board views
    carry — so the help and the menu hub link into each other and cannot
    drift apart (#62).
    """
    return KeyboardReply(HELP_TEXT, {"inline_keyboard": [[_main_menu_button()]]})


# Static command table: the ungated entry points, now
# KeyboardReply-capable (#62) — /start the intro plus the hub's keyboard,
# /help the command reference plus a Main-menu button. /login and /whoami
# are explicit special cases in make_dispatch (they need the auth state
# and the sender's own chat id); the other store-backed commands
# (/projects, /tasks, /task, /attachment, /move, /add, /subscribe,
# /unsubscribe) live in COMMAND_TABLE — they read (or, for /move and
# /add, write) the board; state-change notifications are pushed by the
# Notifier on every poll cycle. Later features extend the dispatch layer
# without changing the poll loop.
COMMANDS: dict[str, Reply] = {
    "/start": start_view(),
    "/help": help_view(),
}


def _flip_toggle(
    rows: list, project_id: int, old_payload: str, subscribed: bool
) -> list:
    """The task-view keyboard after a toggle press.

    The button whose ``callback_data`` is ``old_payload`` (the button that
    was pressed) is replaced in place by the toggle button for the new
    ``subscribed`` state; every other row (the attachment buttons) is
    preserved as-is. A keyboard with no such button — missing or stale from
    an older bot version — falls back to a single row with just the new
    toggle button.
    """
    new_button = _toggle_button(project_id, subscribed)
    new_rows = []
    flipped = False
    for row in rows:
        new_row = []
        for button in row:
            if not flipped and button.get("callback_data") == old_payload:
                new_row.append(new_button)
                flipped = True
            else:
                new_row.append(button)
        new_rows.append(new_row)
    if not flipped:
        return [[new_button]]
    return new_rows


def _state_button_rows(project_id: int, number: int, current_state: str) -> list:
    """Workflow-state buttons for the task detail keyboard.

    One button per state in ``db.WORKFLOW_STATES`` order, the task's
    current state excluded, packed 3 per row. Payload
    ``m:<project-id>:<number>:<idx>`` where ``idx`` is the state's index
    in ``db.WORKFLOW_STATES`` (state names contain spaces, which would
    break the ``:``-separated payload syntax); the ``m:`` family is
    answered by :func:`make_callback_dispatch`.
    """
    buttons = [
        {"text": state, "callback_data": f"m:{project_id}:{number}:{i}"}
        for i, state in enumerate(db.WORKFLOW_STATES)
        if state != current_state
    ]
    return [buttons[i : i + 3] for i in range(0, len(buttons), 3)]


# Markdown → HTML fallback converter (#100): the pure function the HTML
# fallback leg of the rich → HTML → plain chain (#101) sends through. It
# escapes every text fragment (``& < > "`` → the Bot API's named entities)
# before any tag is emitted, then maps the web renderer's markdown subset
# (``yask/web/js/markdown.js``) onto the tag set Telegram's HTML parse mode
# actually supports: strong, em, del, code, pre, a and blockquote. It never
# emits the unsupported tags (h1–h6, ul, ol, li, p, br, …) — those risk a
# 400 that would kill the whole HTML leg and drop to plain — and it
# composes only what the Bot API's entity-nesting rules allow (code spans
# are strict leaves; blockquote content carries no code or links).

_FENCE_RE = re.compile(r"^```")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UL_RE = re.compile(r"^([-*+])\s+(.*)$")
_OL_RE = re.compile(r"^(\d+[.)])\s+(.*)$")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_STRONG_STAR_RE = re.compile(r"\*\*([^*]+)\*\*")
_EM_STAR_RE = re.compile(r"(^|[^*])\*([^*\n]+)\*")
_STRONG_UNDER_RE = re.compile(r"__([^_]+)__")
_EM_UNDER_RE = re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)")
_STRIKE_RE = re.compile(r"~~([^~]+)~~")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?:[^)\s]+)\)")
_CODE_SPAN_PLACEHOLDER_RE = re.compile("\x00(\d+)\x00")


def _escape_html(s: str) -> str:
    """Escape ``& < > "`` as the Bot API's named entities (``&`` first).

    The exact set the web renderer escapes and the Bot API accepts; applied
    to a fragment before any surrounding tag is emitted, so markup in the
    source stays inert server-side.
    """
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _inline_html(s: str, links: bool = True, code: bool = True) -> str:
    """One escaped line's inline pipeline (``s`` must be pre-escaped).

    Code spans are stashed as placeholders before the emphasis passes so
    nothing matches inside them, and restored as ``<code>`` leaves at the
    end. With ``code=False`` code-span markup stays literal, with
    ``links=False`` link markup stays literal — the blockquote context
    passes both off, since the Bot API forbids ``<code>`` and ``<a>`` inside
    ``<blockquote>``. A link whose text carries a stashed code span is left
    literal as a whole (no ``<code>`` inside ``<a>``); the restored code
    span then reads ``[<code>…</code>](url)`` with the brackets plain.
    """
    spans: list[str] = []

    def stash(match: re.Match) -> str:
        spans.append(match.group(1))
        return f"\x00{len(spans) - 1}\x00"

    if code:
        s = _CODE_SPAN_RE.sub(stash, s)
    s = _STRONG_STAR_RE.sub(r"<strong>\1</strong>", s)
    s = _EM_STAR_RE.sub(r"\1<em>\2</em>", s)
    s = _STRONG_UNDER_RE.sub(r"<strong>\1</strong>", s)
    s = _EM_UNDER_RE.sub(r"<em>\1</em>", s)
    s = _STRIKE_RE.sub(r"<del>\1</del>", s)

    def link(match: re.Match) -> str:
        if "\x00" in match.group(1):
            return match.group(0)  # code span in link text: stay literal
        return f'<a href="{match.group(2)}">{match.group(1)}</a>'

    if links:
        s = _LINK_RE.sub(link, s)
    if code:
        s = _CODE_SPAN_PLACEHOLDER_RE.sub(
            lambda m: f"<code>{spans[int(m.group(1))]}</code>", s
        )
    return s


def markdown_to_html(text: str) -> str:
    """Convert the project's markdown subset to Telegram's HTML parse mode.

    The pure, deterministic converter behind the HTML fallback leg of the
    rich → HTML → plain chain (no I/O, no Store, no Bot API): it maps the
    same subset the web renderer (``yask/web/js/markdown.js``) renders onto
    the tag set Telegram's HTML parse mode actually supports —
    ``<strong>``, ``<em>``, ``<del>``, ``<code>``, ``<pre><code>``,
    ``<a href>`` and ``<blockquote>`` — and never the unsupported
    ``h1``–``h6``/``ul``/``ol``/``li``/``p``/``br`` tags, which risk a 400
    that would kill the whole HTML leg and drop to plain.

    Every text fragment is HTML-escaped (``& < > "`` → named entities)
    before any tag is emitted, so markup in an attachment or description
    (e.g. a ``<script>``) stays inert when the Bot API parses the payload
    server-side. The mapping: ``**bold**``/``__bold__`` → ``<strong>``,
    ``*italic*`` → ``<em>``, ``_italic_`` → ``<em>`` (word-boundary
    guarded, so ``snake_case`` identifiers stay literal — a superset over
    the web renderer), ``~~strike~~`` → ``<del>``, inline code →
    ``<code>``, fenced code blocks → ``<pre><code>`` (an unclosed fence
    closes at EOF, web-renderer parity; an empty fence emits a blank line,
    never an empty tag), ``[text](url)`` → ``<a href="url">text</a>`` with
    the URL restricted to ``http``/``https`` (case-sensitive, as in the web
    renderer; ``target``/``rel`` are not supported by the Bot API), and
    ``> quote`` lines → one ``<blockquote>`` per line (an empty one a
    blank line).

    What stays literal — the fallback is the degraded leg, and literal
    markup equals today's plain behavior: headings (``#``…``######``),
    lists (``-``/``*``/``+`` and ``1.``/``1)``), tables and dividers —
    their markers are preserved and the text after a marker is still
    inline-converted; links with any other scheme (``ftp:``, ``tg:``,
    ``javascript:``, …); and a link whose text carries a code span.
    Composition follows the Bot API's entity-nesting rules: code spans are
    strict leaves (no emphasis or links inside them), and the blockquote
    context applies no code or link conversion (the API forbids
    ``<code>``/``<a>`` inside ``<blockquote>``). Blank lines are preserved
    — with no ``<p>``, the newlines are the paragraph separator — and
    CRLF/CR input is normalized to LF, as in the web renderer.
    """
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    in_code = False
    code_lines: list[str] = []
    for line in lines:
        if _FENCE_RE.match(line):
            if in_code:
                body = "\n".join(code_lines)
                out.append(
                    f"<pre><code>{_escape_html(body)}</code></pre>"
                    if body
                    else ""
                )
                code_lines = []
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(line)
            continue
        quote = _QUOTE_RE.match(line)
        if quote:
            content = quote.group(1)
            out.append(
                "<blockquote>"
                f"{_inline_html(_escape_html(content), links=False, code=False)}"
                "</blockquote>"
                if content.strip()
                else ""
            )
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            out.append(
                heading.group(1)
                + " "
                + _inline_html(_escape_html(heading.group(2)))
            )
            continue
        ul = _UL_RE.match(line)
        ol = _OL_RE.match(line)
        if ul or ol:
            match = ul or ol
            out.append(
                match.group(1) + " " + _inline_html(_escape_html(match.group(2)))
            )
            continue
        out.append(_inline_html(_escape_html(line)))
    if in_code:
        body = "\n".join(code_lines)
        out.append(f"<pre><code>{_escape_html(body)}</code></pre>" if body else "")
    return "\n".join(out)


def format_task_view(
    task: dict,
    project: dict,
    history: list[dict],
    chat_id: Optional[int] = None,
    subscribed: bool = False,
) -> Reply:
    """The ``/task`` detail reply for one task.

    The view is a single markdown string built as blocks joined by one
    blank line:

    1. The header block — the task as an H1 heading (``# <n> <title> —
       <type>``), then ``**State:**``, ``**Estimate:**`` (``%g``, only
       when set) and ``**Parent:**`` (only when set) lines, with no blank
       lines between them.
    2. The description, embedded **raw** (agent-written markdown —
       headings, lists, code blocks, tables — is meant to render): cut to
       :data:`DESCRIPTION_MAX` with a ``… (truncated, N chars total)``
       note as its own paragraph when longer (the cut also closes a fence
       it left open, so the unclosed code block cannot swallow the rest
       of the view in the rich renderer or the HTML fallback).
    3. A ``**Prerequisites:**`` list — one ``- #<n> <title> — <state>``
       line per prerequisite (only when present).
    4. An ``**Attachments:**`` list — one
       ``- <id>. <filename> (<size>) — /attachment <project> <number>
       <id>`` line per attachment (only when present).
    5. A ``**History:**`` list — an earlier-count note when
       :data:`HISTORY_MAX` is exceeded, then one line per visible row
       (creation rows, ``from_state`` NULL, render as ``created``) (only
       when present).

    The description cap leaves headroom for the rest of the view under
    the Rich Message budget (:data:`RICH_MESSAGE_MAX`); a pathological
    overflow 400s the rich leg and degrades through the fallback chain,
    whose HTML/plain legs re-truncate at send time.

    Without a ``chat_id`` (the pure formatter) the reply is that markdown
    as a plain :class:`str`, with no keyboard. With a ``chat_id`` it is a
    :class:`RichReply` carrying the markdown and the inline keyboard: one
    row per attachment (label = filename, payload
    ``a:<project-id>:<number>:<attachment-id>``, answered by
    :func:`make_callback_dispatch`) in id order, then the workflow-state
    rows (:func:`_state_button_rows` — hidden on an archived task), then
    the subscribe/unsubscribe toggle row (:func:`_toggle_button`, driven
    by ``subscribed``), then the Main-menu row (:func:`_main_menu_button`,
    payload ``h``). The keyboard always has at least one row (the toggle,
    and with it the Main-menu row), so the Bot API's
    empty-inline-keyboard rejection never triggers.
    """
    header = [f"# {task['number']} {task['title']} — {task['type']}"]
    header.append(f"**State:** {task['state']}")
    if task["estimate"] is not None:
        header.append(f"**Estimate:** {task['estimate']:g}")
    if task["parent_number"] is not None:
        header.append(f"**Parent:** #{task['parent_number']}")
    blocks = ["\n".join(header)]
    description = task["description"] or ""
    if description.strip():
        if len(description) > DESCRIPTION_MAX:
            cut = description[:DESCRIPTION_MAX]
            fence_lines = sum(
                1 for line in cut.split("\n") if _FENCE_RE.match(line)
            )
            if fence_lines % 2 == 1:
                # the cut left a code fence open: close it, or the
                # unclosed block swallows the note and every section
                # after it (rich renderer and markdown_to_html alike)
                cut += "\n```"
            description = (
                f"{cut}\n\n… (truncated, {len(description)} chars total)"
            )
        blocks.append(description)
    if task["prerequisites"]:
        blocks.append(
            "**Prerequisites:**\n"
            + "\n".join(
                f"- #{p['number']} {p['title']} — {p['state']}"
                for p in task["prerequisites"]
            )
        )
    if task["attachments"]:
        blocks.append(
            "**Attachments:**\n"
            + "\n".join(
                f"- {a['id']}. {a['filename']} ({_human_size(a['size'])}) — "
                f"/attachment {project['name']} {task['number']} {a['id']}"
                for a in task["attachments"]
            )
        )
    if history:
        lines = []
        if len(history) > HISTORY_MAX:
            lines.append(f"- … {len(history) - HISTORY_MAX} earlier transitions")
        for h in history[-HISTORY_MAX:]:
            if h["from_state"] is None:
                lines.append(f"- {h['changed_at']} — created ({h['source']})")
            else:
                lines.append(
                    f"- {h['changed_at']} — {h['from_state']} → {h['to_state']} "
                    f"({h['source']})"
                )
        blocks.append("**History:**\n" + "\n".join(lines))
    text = "\n\n".join(blocks)
    if chat_id is None:
        return text
    rows = [
        [
            {
                "text": a["filename"],
                "callback_data": (
                    f"a:{project['id']}:{task['number']}:{a['id']}"
                ),
            }
        ]
        for a in task["attachments"]
    ]
    # Blocked keeps its state rows (moving it to a workflow state is the
    # natural "resume" action); Archived never shows them (the sanctioned
    # way out of Archived is the separate restore action).
    if task["state"] != db.ARCHIVED_STATE:
        rows.extend(
            _state_button_rows(project["id"], task["number"], task["state"])
        )
    rows.append([_toggle_button(project["id"], subscribed)])
    rows.append([_main_menu_button()])
    return RichReply(text, {"inline_keyboard": rows})


def _resolve_task(store: Store, project: dict, ref: str) -> Union[str, dict]:
    """Resolve a task reference of ``project``: number first, then title.

    An all-digit reference looks up the task number first (a real number
    always wins); when no such task exists it falls back to the title
    search, so a task literally titled "2024" is still findable. Otherwise
    the reference is an exact, case-insensitive title match (archived tasks
    excluded): zero matches is a not-found reply, several matches a
    disambiguation list with numbers, one match the task itself. Returns
    the serialized task dict or a ready-to-send reply string.
    """
    if ref.isdigit():
        try:
            return store.get_task(project["id"], int(ref))
        except NotFound:
            pass  # the task may be titled with digits
    matches = store.find_tasks_by_title(project["id"], ref)
    if not matches:
        if ref.isdigit():
            return f"Task #{ref} not found in {project['name']}."
        return f"Task '{ref}' not found in {project['name']}."
    if len(matches) > 1:
        lines = [f"Several tasks in {project['name']} match '{ref}':"]
        lines.extend(f"  #{m['number']} {m['title']} — {m['state']}" for m in matches)
        lines.append(f"Use /task {project['name']} <number>.")
        return "\n".join(lines)
    return store.get_task(project["id"], matches[0]["number"])


def task_view(
    store: Store, arg: Optional[str], chat_id: Optional[int] = None
) -> Reply:
    """Format the ``/task <project> <number|title>`` reply.

    The argument mixes a project reference and a task reference, either of
    which may contain spaces; :func:`_split_project` resolves the longest
    project prefix, the rest is the task (number or case-insensitive
    title). No argument, or no task reference after the project, gets the
    usage text; an unresolvable project gets the not-found reply pointing
    at ``/projects``.

    With a ``chat_id`` the reply is the task view's rich form: the markdown
    detail as a :class:`RichReply` carrying the view's keyboard, whose
    subscribe/unsubscribe toggle button reflects whether the chat is
    currently subscribed to the project (computed from
    ``store.list_subscriptions``). Without one, the plain text form is
    returned.
    """
    if arg is None or not arg.strip():
        return TASK_USAGE_TEXT
    words = arg.split()
    project, rest = _split_project(store, words)
    if project is None:
        return (
            f"Project '{words[0]}' not found. Use /projects to list projects."
        )
    if not rest:
        return TASK_USAGE_TEXT
    task = _resolve_task(store, project, " ".join(rest))
    if not isinstance(task, dict):
        return task
    history = store.get_history(project["id"], task["number"])
    subscribed = False
    if chat_id is not None:
        subscribed = any(
            s["project_id"] == project["id"]
            for s in store.list_subscriptions(chat_id)
        )
    return format_task_view(task, project, history, chat_id, subscribed)


def _attachment_caption(task: dict, meta: dict) -> str:
    """The ``#<n> <title> — <filename>`` context line shared by the inline
    attachment message header and the file-reply caption."""
    return f"#{task['number']} {task['title']} — {meta['filename']}"


def _truncate_inline(
    text: str, total: int, budget: int = INLINE_TEXT_MAX
) -> str:
    """``text`` truncated to ``budget`` code points (default
    :data:`INLINE_TEXT_MAX`) with a ``… (truncated, N chars total)`` note,
    where ``total`` is the pre-truncation content length (the body,
    excluding the note)."""
    if len(text) <= budget:
        return text
    note = f"\n… (truncated, {total} chars total)"
    room = max(0, budget - len(note))
    return text[:room] + note


def attachment_reply(
    meta: dict, data: bytes, task: dict
) -> Union[str, FileReply, RichReply]:
    """The reply for one attachment.

    A ``text/markdown`` attachment under :data:`INLINE_MARKDOWN_MAX_SIZE`
    comes back as a :class:`RichReply` (sent with ``sendRichMessage``,
    falling back through the rich → HTML → plain chain on failure): the
    same ``#<number> <title> — <filename>`` context line the file caption
    uses, then the **raw** markdown body. The 16 KiB threshold fits the
    Rich Message budget (:data:`RICH_MESSAGE_MAX`), so the rich payload is
    never truncated; the fallback legs re-truncate at send time.

    A ``text/plain`` attachment under the same threshold stays a plain
    ``str`` — the decoded content prefixed with the context line,
    truncated to :data:`INLINE_TEXT_MAX` with a ``… (truncated, N chars
    total)`` note (plain text is deliberately not formatted). Everything
    else is a :class:`FileReply` — images as a photo (already displayed
    inline by Telegram), other content as a document.

    The caption prefix and the truncation note are produced by
    :func:`_attachment_caption` and :func:`_truncate_inline`.
    """
    if meta["size"] < INLINE_MARKDOWN_MAX_SIZE:
        if meta["content_type"] == "text/markdown":
            body = data.decode("utf-8", errors="replace")
            return RichReply(f"{_attachment_caption(task, meta)}\n{body}")
        if meta["content_type"] == "text/plain":
            body = data.decode("utf-8", errors="replace")
            return _truncate_inline(
                f"{_attachment_caption(task, meta)}\n{body}", len(body)
            )
    return FileReply(
        filename=meta["filename"],
        data=data,
        content_type=meta["content_type"],
        caption=_attachment_caption(task, meta),
    )


def attachment_view(
    store: Store, arg: Optional[str]
) -> Union[str, FileReply, RichReply]:
    """Resolve ``/attachment <project> <task> <attachment-id>``.

    Project by longest prefix, task by number or title (a disambiguation
    list when the title is ambiguous — never an attachment send), then the
    attachment id (the last word) is looked up scoped to that task. Returns
    the attachment's reply — :func:`attachment_reply`: a rich message for
    small markdown attachments, an inline text reply for small plain text,
    a :class:`FileReply` for images (photo) and larger content (document)
    — or a usage / not-found text.
    """
    if arg is None or not arg.strip():
        return ATTACHMENT_USAGE_TEXT
    words = arg.split()
    project, rest = _split_project(store, words)
    if project is None:
        return (
            f"Project '{words[0]}' not found. Use /projects to list projects."
        )
    if len(rest) < 2:
        return ATTACHMENT_USAGE_TEXT
    id_ref, task_ref = rest[-1], " ".join(rest[:-1])
    task = _resolve_task(store, project, task_ref)
    if not isinstance(task, dict):
        return task
    if not id_ref.isdigit():
        return (
            f"Attachment '{id_ref}' not found on task #{task['number']} "
            f"({project['name']}). Use /task {project['name']} {task['number']} "
            "to list the task's attachments."
        )
    try:
        meta, data = store.get_task_attachment(
            project["id"], task["number"], int(id_ref)
        )
    except NotFound:
        return (
            f"Attachment {id_ref} not found on task #{task['number']} "
            f"({project['name']}). Use /task {project['name']} {task['number']} "
            "to list the task's attachments."
        )
    return attachment_reply(meta, data, task)


def _match_state_suffix(words: list[str]) -> Optional[tuple[list[str], str]]:
    """The suffix of ``words`` that is a workflow state name.

    Case-insensitive match against :data:`db.WORKFLOW_STATES`, shortest
    suffix first — and since no state name is a suffix of another state
    name, at most one suffix can ever match, so the search order is not
    semantically significant. A state-only argument never matches (the
    matched suffix is always proper, so a task reference is required).
    Returns ``(leading words, matched state)`` — the leading words are the
    task reference — or None when no suffix matches.
    """
    for i in range(len(words), 0, -1):
        candidate = " ".join(words[i:])
        for state in db.WORKFLOW_STATES:
            if candidate.lower() == state.lower():
                return words[:i], state
    return None


def _match_type_suffix(
    words: list[str], type_names: list[str]
) -> tuple[list[str], Optional[str]]:
    """Split ``words`` into ``(title words, type name)`` on a trailing
    ``as <type>`` segment.

    The **last** word equal to ``as`` (case-insensitive) is the split
    point: the words after it, joined, must case-insensitively equal one of
    ``type_names`` — then the matched type's canonical name is returned and
    the words before the split are the title. Otherwise (no ``as`` at all,
    a trailing ``as`` with nothing after it, or a segment that matches no
    type) the whole ``words`` are the title and the type is ``None`` (the
    caller applies the default). Only the last ``as`` counts, so a title
    such as ``a as b as Bug`` yields title ``a as b``, type ``Bug``.
    """
    for i in range(len(words) - 1, -1, -1):
        if words[i].lower() != "as":
            continue
        segment = " ".join(words[i + 1 :])
        for name in type_names:
            if segment.lower() == name.lower():
                return words[:i], name
        # The last "as" does not introduce a known type: the whole
        # remainder is the title (titles may contain "as" freely).
        return words, None
    return words, None


def _confirm_move_markup(
    project_id: int, task: dict, target: str, affected: list[dict]
) -> KeyboardReply:
    """The confirm/cancel keyboard for a move that pulls prerequisites along.

    The same prompt both entry points share (the ``/move`` command and the
    ``m:`` state buttons): ``Move #<n> to <target>?`` plus the pulled
    prerequisites (capped at 10 lines with a ``… N more`` note — the
    confirmed move still applies to all of them), a ``Move all`` button
    (payload ``c:<project-id>:<number>:<idx>``) and a ``Cancel`` button
    (payload ``x:<project-id>:<number>``), both answered by
    :func:`make_callback_dispatch`.
    """
    lines = [
        f"Move #{task['number']} to {target}?",
        "This also moves its prerequisites that have not reached this stage:",
    ]
    pulled = affected[1:]
    for a in pulled[:10]:
        lines.append(f"  #{a['number']} {a['title']} — {a['from']}")
    if len(pulled) > 10:
        lines.append(f"  … {len(pulled) - 10} more")
    state_idx = db.WORKFLOW_STATES.index(target)
    return KeyboardReply(
        "\n".join(lines),
        {
            "inline_keyboard": [
                [
                    {
                        "text": "Move all",
                        "callback_data": (
                            f"c:{project_id}:{task['number']}:{state_idx}"
                        ),
                    }
                ],
                [
                    {
                        "text": "Cancel",
                        "callback_data": f"x:{project_id}:{task['number']}",
                    }
                ],
            ]
        },
    )


def move_view(store: Store, arg: Optional[str]) -> Reply:
    """Format the ``/move <project> <task> <state>`` reply.

    The argument mixes a project reference, a task reference (number or
    case-insensitive title) and a target state, any of which may contain
    spaces: :func:`_split_project` resolves the longest project prefix,
    :func:`_match_state_suffix` resolves the state (the matching suffix of
    the remaining words, so multi-word states like ``In progress``
    disambiguate from multi-word titles), and the words between them are
    the task reference (``_resolve_task`` — its disambiguation and
    not-found strings pass through). A single-task move is applied and
    confirmed in plain text; a move that would pull prerequisites along is
    answered with the confirm/cancel keyboard
    (:func:`_confirm_move_markup`) and nothing is written until the
    ``c:`` button is pressed. ``Blocked`` is never a target (a move there
    must carry an ``unblock.md`` attachment — web UI / MCP only) and
    archived tasks are refused.
    """
    if arg is None or not arg.strip():
        return MOVE_USAGE_TEXT
    words = arg.split()
    project, rest = _split_project(store, words)
    if project is None:
        return (
            f"Project '{words[0]}' not found. Use /projects to list projects."
        )
    if not rest:
        return MOVE_USAGE_TEXT
    matched = _match_state_suffix(rest)
    if matched is None:
        return (
            f"Unknown state '{rest[-1]}'. Use one of: "
            + ", ".join(db.WORKFLOW_STATES)
            + "."
        )
    task_ref, target = matched
    task = _resolve_task(store, project, " ".join(task_ref))
    if not isinstance(task, dict):
        return task
    if task["state"] == db.ARCHIVED_STATE:
        return f"Task #{task['number']} is archived and cannot be moved."
    if task["state"] == target:
        return f"Task #{task['number']} is already in {target}."
    affected = store.plan_move(project["id"], task["number"], target)
    if len(affected) > 1:
        return _confirm_move_markup(project["id"], task, target, affected)
    store.move_task(project["id"], task["number"], target)
    return f"Moved #{task['number']} to {target}."


def add_view(store: Store, arg: Optional[str]) -> Reply:
    """Format the ``/add <project> <title> [as <type>]`` reply.

    The argument mixes a project reference and a title, either of which
    may contain spaces: :func:`_split_project` resolves the longest
    project prefix, and the remaining words are the title — unless they
    end in ``as <type>`` for one of the board's task types
    (:func:`_match_type_suffix`), in which case that segment names the
    type. No argument, or a project with no title words left, gets the
    usage text; an unresolvable project gets the not-found reply pointing
    at ``/projects``. On success the store creates a task of the given
    type (default ``Task``) in the project's Backlog (numbering, ordering
    and history included) and the reply is a :class:`KeyboardReply`
    confirmation with the new number and type, carrying a two-row inline
    keyboard: the new task's detail button (label ``#<number> <title>``,
    truncated to :data:`NOTIFICATION_BUTTON_TEXT_MAX` chars, payload
    ``t:<project-id>:<number>`` — answered by :func:`make_callback_dispatch`
    (the ``t:`` handler), which opens the task's detail view) and, under
    it, the Main-menu row (:func:`_main_menu_button`, payload ``h``) —
    the same shape the state-change notifications carry. The failure
    paths (usage, not-found) stay plain ``str``.
    """
    if arg is None or not arg.strip():
        return add_usage_text(store)
    words = arg.split()
    project, rest = _split_project(store, words)
    if project is None:
        return (
            f"Project '{words[0]}' not found. Use /projects to list projects."
        )
    type_names = [t["name"] for t in store.list_task_types()]
    title_words, type_name = _match_type_suffix(rest, type_names)
    if not title_words:
        return add_usage_text(store)
    task = store.create_task(
        project["id"], " ".join(title_words), type=type_name or "Task"
    )
    text = (
        f"Created #{task['number']} '{task['title']}' ({task['type']}) "
        f"in {project['name']} — Backlog. "
        f"Use /task {project['name']} {task['number']} to view it."
    )
    button = {
        "text": _truncate_button_label(f"#{task['number']} {task['title']}"),
        "callback_data": f"t:{project['id']}:{task['number']}",
    }
    return KeyboardReply(
        text, {"inline_keyboard": [[button], [_main_menu_button()]]}
    )


def describe_view(store: Store, arg: Optional[str]) -> Reply:
    """Format the ``/describe <project> <number|title> <description>`` reply.

    Sets — replaces — the task's description (``store.update_task``, the
    same write as the web UI / MCP ``update_task``; no state-history entry).
    The argument mixes a project reference and a task reference, either of
    which may contain spaces: :func:`_split_project` resolves the longest
    project prefix. The task reference splits from the description words
    by form:

    - **Number form** — the first word after the project is all digits: it
      is the task number, resolved strictly with ``store.get_task`` (no
      title fallback — a write command must not resolve a number to a
      differently-titled task), and everything after it is the
      description.
    - **Title form** — otherwise the words are split by longest-prefix
      unique match (mirroring :func:`_split_project`'s longest-prefix
      convention): the longest prefix of the remaining words that matches
      exactly one visible task (case-insensitive title) is the reference,
      the remaining words the description. A prefix that matches several
      tasks gets the disambiguation list (nothing is written); no prefix
      matching at all gets the not-found reply quoting the first word.

    No argument, a project with no task reference left, or a reference
    with no description words left, gets the usage text; an unresolvable
    project gets the not-found reply pointing at ``/projects``. Archived
    tasks: the number form reaches them (``get_task`` sees archived), the
    title form does not (``find_tasks_by_title`` excludes archived) — the
    same asymmetry as ``/task``. On success the reply is a
    :class:`KeyboardReply` confirmation carrying the task's detail button
    (label ``#<number> <title>`` truncated to
    :data:`NOTIFICATION_BUTTON_TEXT_MAX` chars, payload
    ``t:<project-id>:<number>`` — answered by :func:`make_callback_dispatch`
    (the ``t:`` handler)) and, under it, the Main-menu row
    (:func:`_main_menu_button`, payload ``h``) — the same shape the
    ``/add`` confirmation carries. The failure paths (usage, not-found,
    disambiguation) stay plain ``str``.
    """
    if arg is None or not arg.strip():
        return DESCRIBE_USAGE_TEXT
    words = arg.split()
    project, rest = _split_project(store, words)
    if project is None:
        return (
            f"Project '{words[0]}' not found. Use /projects to list projects."
        )
    if not rest:
        return DESCRIBE_USAGE_TEXT
    if rest[0].isdigit():
        # Number form: strict number lookup, the rest is the description.
        try:
            task = store.get_task(project["id"], int(rest[0]))
        except NotFound:
            return f"Task #{rest[0]} not found in {project['name']}."
        description = " ".join(rest[1:])
        if not description:
            return DESCRIBE_USAGE_TEXT
    else:
        # Title form: the longest prefix of the remaining words that
        # matches exactly one task is the reference; the words after it
        # are the description.
        found = None
        for i in range(len(rest), 0, -1):
            ref = " ".join(rest[:i])
            matches = store.find_tasks_by_title(project["id"], ref)
            if not matches:
                continue
            desc_words = rest[i:]
            if not desc_words:
                if len(matches) == 1:
                    return DESCRIBE_USAGE_TEXT  # resolved, nothing to write
                return _resolve_task(store, project, ref)  # disambiguation
            if len(matches) == 1:
                task = store.get_task(project["id"], matches[0]["number"])
                found = (task, " ".join(desc_words))
                break
            return _resolve_task(store, project, ref)  # ambiguous
        if found is None:
            return f"Task '{rest[0]}' not found in {project['name']}."
        task, description = found
    task = store.update_task(
        project["id"], task["number"], description=description
    )
    text = (
        f"Set the description of #{task['number']} '{task['title']}' "
        f"({len(description)} chars). "
        f"Use /task {project['name']} {task['number']} to view it."
    )
    button = {
        "text": _truncate_button_label(f"#{task['number']} {task['title']}"),
        "callback_data": f"t:{project['id']}:{task['number']}",
    }
    return KeyboardReply(
        text, {"inline_keyboard": [[button], [_main_menu_button()]]}
    )


def type_view(store: Store, arg: Optional[str]) -> Reply:
    """Format the ``/type <project> <number|title> <type>`` reply.

    Changes the task's type (``store.update_task``, the same write as the
    web UI / MCP ``update_task``). The argument mixes a project reference
    and a task reference, either of which may contain spaces:
    :func:`_split_project` resolves the longest project prefix. The task
    reference splits from the type-name words by form:

    - **Number form** — the first word after the project is all digits: it
      is the task number, resolved strictly with ``store.get_task`` (no
      title fallback — a write command must not resolve a number to a
      differently-titled task), and the words after it are the type name.
    - **Title form** — otherwise the words are split by longest-prefix
      unique match (mirroring :func:`_split_project`'s and
      :func:`describe_view`'s conventions): the longest prefix of the
      remaining words that matches exactly one visible task
      (case-insensitive title) is the reference, the remaining words the
      type name. A prefix that matches several tasks gets the
      disambiguation list (nothing is written); no prefix matching at all
      gets the not-found reply quoting the first word. (A task whose title
      ends in words that also read like a type keeps the longer reference
      — the longest-prefix-first rule, as in ``/describe``.)

    The type name is matched case-insensitively against the board's task
    types (``store.list_task_types`` — the store's own lookup is
    case-insensitive too): the matched type's canonical name is written, an
    unmatched name gets the ``Unknown type '<X>'. Use one of: <types>.``
    reply (the type list is board-driven, so custom types are discoverable),
    and naming the task's current type answers the "already of type" notice
    (the ``/move`` "already in <state>" mirror). The store enforces the
    domain rules itself (a task with children cannot be demoted from an
    epic; switching to an epic nulls the estimate) — its
    ``ValidationError`` is a domain message, not a store failure, so it is
    returned to the user verbatim, and any other exception propagates to
    the dispatcher's try/except (the row's error text). No argument, a
    project with no task reference left, or a reference with no type words
    left, gets the usage text; an unresolvable project gets the not-found
    reply pointing at ``/projects``. Archived tasks: the number form
    reaches them (``get_task`` sees archived), the title form does not
    (``find_tasks_by_title`` excludes archived) — the same asymmetry as
    ``/task``. On success the reply is a :class:`KeyboardReply`
    confirmation carrying the task's detail button (label
    ``#<number> <title>`` truncated to
    :data:`NOTIFICATION_BUTTON_TEXT_MAX` chars, payload
    ``t:<project-id>:<number>`` — answered by :func:`make_callback_dispatch`
    (the ``t:`` handler)) and, under it, the Main-menu row
    (:func:`_main_menu_button`, payload ``h``) — the same shape the
    ``/add`` and ``/describe`` confirmations carry. The failure paths
    (usage, not-found, disambiguation) stay plain ``str``.
    """
    if arg is None or not arg.strip():
        return type_usage_text(store)
    words = arg.split()
    project, rest = _split_project(store, words)
    if project is None:
        return (
            f"Project '{words[0]}' not found. Use /projects to list projects."
        )
    if not rest:
        return type_usage_text(store)
    if rest[0].isdigit():
        # Number form: strict number lookup, the rest is the type name.
        try:
            task = store.get_task(project["id"], int(rest[0]))
        except NotFound:
            return f"Task #{rest[0]} not found in {project['name']}."
        type_words = rest[1:]
    else:
        # Title form: the longest prefix of the remaining words that
        # matches exactly one task is the reference; the words after it
        # are the type name.
        found = None
        for i in range(len(rest), 0, -1):
            ref = " ".join(rest[:i])
            matches = store.find_tasks_by_title(project["id"], ref)
            if not matches:
                continue
            type_words = rest[i:]
            if not type_words:
                if len(matches) == 1:
                    return type_usage_text(store)  # resolved, nothing to write
                return _resolve_task(store, project, ref)  # disambiguation
            if len(matches) == 1:
                task = store.get_task(project["id"], matches[0]["number"])
                found = (task, type_words)
                break
            return _resolve_task(store, project, ref)  # ambiguous
        if found is None:
            return f"Task '{rest[0]}' not found in {project['name']}."
        task, type_words = found
    if not type_words:
        return type_usage_text(store)
    type_ref = " ".join(type_words)
    types = store.list_task_types()
    canonical = next(
        (t["name"] for t in types if t["name"].lower() == type_ref.lower()),
        None,
    )
    if canonical is None:
        return (
            f"Unknown type '{type_ref}'. Use one of: "
            + ", ".join(t["name"] for t in types)
            + "."
        )
    if task["type"].lower() == canonical.lower():
        return f"Task #{task['number']} is already of type {task['type']}."
    try:
        task = store.update_task(project["id"], task["number"], type=canonical)
    except ValidationError as e:
        return str(e)
    text = (
        f"Changed the type of #{task['number']} '{task['title']}' "
        f"to {task['type']}. "
        f"Use /task {project['name']} {task['number']} to view it."
    )
    button = {
        "text": _truncate_button_label(f"#{task['number']} {task['title']}"),
        "callback_data": f"t:{project['id']}:{task['number']}",
    }
    return KeyboardReply(
        text, {"inline_keyboard": [[button], [_main_menu_button()]]}
    )


def subscribe_view(store: Store, chat_id: int, arg: Optional[str] = None) -> str:
    """Format the ``/subscribe [project]`` reply.

    Without an argument, lists the chat's current subscriptions (``{id}.
    {name}`` lines in name order, or ``(none)``) — the same output shape as
    ``/projects``. With an argument, subscribes the chat to the resolved
    project (case-insensitive name or integer id); an unresolvable argument
    yields the same not-found reply as ``/tasks``.
    """
    if arg is None:
        subs = store.list_subscriptions(chat_id)
        if not subs:
            return "Your subscriptions:\n(none)"
        lines = ["Your subscriptions:"]
        for s in subs:
            lines.append(f"{s['project_id']}. {s['project_name']}")
        return "\n".join(lines)

    project = _resolve_project(store, arg)
    if project is None:
        return f"Project '{arg}' not found. Use /projects to list projects."
    sub = store.subscribe_project(chat_id, project["id"])
    return (
        f"Subscribed to {sub['project_name']} ({sub['project_id']}) — "
        "you will be notified about task state changes in this project."
    )


def unsubscribe_view(store: Store, chat_id: int, arg: str) -> str:
    """Format the ``/unsubscribe <project>`` reply.

    Resolves the project (case-insensitive name or integer id) and removes
    the chat's subscription: a confirmation when a row was removed, a
    "not subscribed" notice when there was nothing to remove, and the
    ``/tasks`` not-found text for an unresolvable argument.
    """
    project = _resolve_project(store, arg)
    if project is None:
        return f"Project '{arg}' not found. Use /projects to list projects."
    result = store.unsubscribe_project(chat_id, project["id"])
    if result["removed"]:
        return f"Unsubscribed from {project['name']} ({project['id']})."
    return f"You are not subscribed to {project['name']} ({project['id']})."


def _truncate_button_label(
    label: str, max_len: int = NOTIFICATION_BUTTON_TEXT_MAX
) -> str:
    """A button label capped at ``max_len`` chars (char-based).

    The label goes out unchanged when it already fits; otherwise it is cut
    to ``max_len - 1`` chars and suffixed with an ellipsis (``…``), so the
    total length never exceeds ``max_len``.
    """
    if len(label) <= max_len:
        return label
    return f"{label[:max_len - 1]}…"


def format_notification(change: dict) -> KeyboardReply:
    """One notification message for a state-history change.

    The text is ``{project_name}: #{number} {title} — {from_state} →
    {to_state} (/task {project_id} {number})`` — the trailing ``/task``
    reference follows the ``/tasks`` drill-down convention and is answered
    by the ``/task`` command. The message carries a two-row inline
    keyboard: the task button (label ``#<number> <title>``, truncated to
    :data:`NOTIFICATION_BUTTON_TEXT_MAX` chars, payload
    ``t:<project_id>:<number>`` — answered by :func:`make_callback_dispatch`
    (the ``t:`` handler), which opens the task's detail view) and, under
    it, the Main-menu row (:func:`_main_menu_button`, payload ``h``), so
    the menu hub is reachable from the notification too (#61). No
    subscribe/unsubscribe toggle: the Notifier only fans out to subscribed
    chats, so the receiving chat is subscribed by definition.
    """
    text = (
        f"{change['project_name']}: #{change['number']} {change['title']} — "
        f"{change['from_state']} → {change['to_state']} "
        f"(/task {change['project_id']} {change['number']})"
    )
    button = {
        "text": _truncate_button_label(f"#{change['number']} {change['title']}"),
        "callback_data": f"t:{change['project_id']}:{change['number']}",
    }
    return KeyboardReply(
        text, {"inline_keyboard": [[button], [_main_menu_button()]]}
    )


# The dispatch table: one row per store-backed command — its handler
# (uniform signature (store, text, chat_id): the board or subscription
# view for the command) and the error text the dispatcher replies with
# when the handler raises (the per-command family constant).
# make_dispatch is table-driven over this: it tokenizes, special-cases
# /login and /whoami (they need the auth state / the sender chat id),
# gates the table's commands on auth, runs the handler in try/except and
# falls back to reply_for. The import-time check below pins the table to
# the registry's auth_gated set, so a command added to the registry
# without a handler (or vice versa) fails the import instead of silently
# never working.
CommandHandler = Callable[[Store, Optional[str], Optional[int]], Reply]


def _handle_projects(
    store: Store, text: Optional[str], chat_id: Optional[int]
) -> Reply:
    """``/projects``: the project list view (arguments ignored)."""
    return project_view(store)


def _handle_tasks(
    store: Store, text: Optional[str], chat_id: Optional[int]
) -> Reply:
    """``/tasks``: the tasks view for the (optional) project argument."""
    return tasks_view(store, _tasks_arg(text))


def _handle_backlog(
    store: Store, text: Optional[str], chat_id: Optional[int]
) -> Reply:
    """``/backlog``: the Backlog view for the (optional) project argument."""
    return backlog_view(store, _tasks_arg(text))


def _handle_task(
    store: Store, text: Optional[str], chat_id: Optional[int]
) -> Reply:
    """``/task``: one task's detail view (keyboard form for a sender)."""
    return task_view(store, _tasks_arg(text), chat_id)


def _handle_attachment(
    store: Store, text: Optional[str], chat_id: Optional[int]
) -> Reply:
    """``/attachment``: one of a task's attachments (text, photo or file)."""
    return attachment_view(store, _tasks_arg(text))


def _handle_move(
    store: Store, text: Optional[str], chat_id: Optional[int]
) -> Reply:
    """``/move``: move a task to a state (confirm keyboard on cascades)."""
    return move_view(store, _tasks_arg(text))


def _handle_add(
    store: Store, text: Optional[str], chat_id: Optional[int]
) -> Reply:
    """``/add``: create a task in the project's backlog."""
    return add_view(store, _tasks_arg(text))


def _handle_describe(
    store: Store, text: Optional[str], chat_id: Optional[int]
) -> Reply:
    """``/describe``: set a task's description (replace semantics)."""
    return describe_view(store, _tasks_arg(text))


def _handle_type(
    store: Store, text: Optional[str], chat_id: Optional[int]
) -> Reply:
    """``/type``: change a task's type (store-enforced domain rules)."""
    return type_view(store, _tasks_arg(text))


def _handle_attach(
    store: Store, text: Optional[str], chat_id: Optional[int]
) -> Reply:
    """``/attach`` typed as a plain text message (no file): the usage text.

    The real ``/attach`` path is the async file handler of
    :func:`make_dispatch` (a document or photo whose caption carries the
    command); this row exists so the command is gated and listed like
    every other board command (``_verify_dispatch_table``).
    """
    return ATTACH_USAGE_TEXT


def _handle_subscribe(
    store: Store, text: Optional[str], chat_id: Optional[int]
) -> Reply:
    """``/subscribe``: subscribe the sender's chat (needs the chat id)."""
    if chat_id is None:
        return SUBSCRIBE_ERROR_TEXT
    return subscribe_view(store, chat_id, _tasks_arg(text))


def _handle_unsubscribe(
    store: Store, text: Optional[str], chat_id: Optional[int]
) -> Reply:
    """``/unsubscribe``: stop the sender's chat's notifications
    (needs the chat id)."""
    if chat_id is None:
        return UNSUBSCRIBE_ERROR_TEXT
    return unsubscribe_view(store, chat_id, _tasks_arg(text))


COMMAND_TABLE: dict[str, tuple[CommandHandler, str]] = {
    "/projects": (_handle_projects, PROJECTS_ERROR_TEXT),
    "/tasks": (_handle_tasks, TASKS_ERROR_TEXT),
    "/backlog": (_handle_backlog, BACKLOG_ERROR_TEXT),
    "/task": (_handle_task, TASK_ERROR_TEXT),
    "/attachment": (_handle_attachment, ATTACHMENT_ERROR_TEXT),
    "/attach": (_handle_attach, ATTACH_ERROR_TEXT),
    "/move": (_handle_move, MOVE_ERROR_TEXT),
    "/add": (_handle_add, ADD_ERROR_TEXT),
    "/describe": (_handle_describe, DESCRIBE_ERROR_TEXT),
    "/type": (_handle_type, TYPE_ERROR_TEXT),
    "/subscribe": (_handle_subscribe, SUBSCRIBE_ERROR_TEXT),
    "/unsubscribe": (_handle_unsubscribe, UNSUBSCRIBE_ERROR_TEXT),
}


def _verify_dispatch_table() -> None:
    """Fail the import if COMMAND_TABLE and COMMAND_REGISTRY diverge.

    The table must cover exactly the registry's auth_gated commands: a
    gated command without a handler would silently never work (the old
    dual-source-of-truth bug), and a handler for a command the registry
    does not gate would run unauthenticated.
    """
    gated = {c.name for c in COMMAND_REGISTRY if c.auth_gated}
    if set(COMMAND_TABLE) != gated:
        raise ValueError(
            "COMMAND_TABLE diverges from COMMAND_REGISTRY: "
            f"unhandled gated commands: {sorted(gated - set(COMMAND_TABLE))}, "
            f"handlers for unknown commands: {sorted(set(COMMAND_TABLE) - gated)}"
        )


_verify_dispatch_table()


def make_dispatch(
    store: Store,
    auth: Optional[Auth] = None,
    api: Optional[BotAPI] = None,
) -> Callable[..., Optional[Union[Reply, Awaitable[Reply]]]]:
    """Build the message→reply dispatcher for a bot bound to ``store``.

    Store-backed commands — the rows of :data:`COMMAND_TABLE`
    (``/projects``, ``/tasks``, ``/backlog``, ``/task``, ``/attachment``,
    ``/attach``, ``/move``, ``/add``, ``/describe``, ``/type``,
    ``/subscribe``, ``/unsubscribe``) — read the board through ``store``
    via their table handler (``/attach`` typed as a plain text message
    answers its usage text; its real path is the file handler below); the
    subscription
    commands additionally need the sender's chat id, hence
    ``dispatch(text, chat_id)``. ``/login`` and ``/whoami`` are explicit
    special cases before the table lookup (they need the auth state and
    the sender's own chat id). Everything else falls back to the static
    :func:`reply_for`. A failure reading (or writing) the store yields
    the command's row error text instead of crashing the long-poll loop.
    ``/task`` resolves to a text reply and ``/attachment`` to an inline
    text reply (small text attachments) or a :class:`FileReply` (the
    attachment bytes for a file send); a :class:`KeyboardReply` is the
    same text-plus-keyboard shape for inline-keyboard views — ``/move``
    answers with one when the move would pull prerequisites along (the
    confirm/cancel keyboard, nothing applied until the ``c:`` button),
    and ``/add``, ``/describe`` and ``/type`` answer with the
    confirmation keyboard (the task's detail button plus the Main-menu row).

    File messages (a document or a photo, :class:`IncomingFile`) are
    answered by an **async** file handler — the ``/attach`` caption
    flow: it downloads the file from the Bot API via ``api`` and stores
    it on the resolved task through ``store.add_attachment``.
    ``dispatch(text, chat_id, file)`` returns that handler's coroutine
    for a file message (the caller — ``run_bot`` — awaits it when the
    reply is a coroutine, ``inspect.iscoroutine``); every text message
    keeps the synchronous path. ``api`` is the :class:`BotAPI` the
    handler downloads through (the production bot always passes one);
    without it a file message answers the attach error text.

    Board access is gated by ``auth`` (an :class:`Auth`; the production
    bot always passes one): every :data:`COMMAND_TABLE` command answers
    unauthenticated chats with :data:`AUTH_REQUIRED_TEXT` and no board
    data — the gate covers exactly the registry's auth_gated commands
    (:func:`_verify_dispatch_table` fails the import on any divergence)
    — and the file handler is gated the same way, before any resolution
    or download. ``/login`` (success/failure indistinguishable for
    unknown chats) and ``/whoami`` (the sender's own chat id) are
    ungated. Without an ``auth`` the commands are open (the legacy,
    unauthenticated behavior).
    """

    def _authed(chat_id: Optional[int]) -> bool:
        return auth is None or auth.is_authenticated(chat_id)

    async def _dispatch_file(file: IncomingFile, chat_id: Optional[int]) -> Reply:
        """The async handler for file messages: the ``/attach`` flow.

        The auth gate comes first (no resolution, no download, no board
        data for an unauthenticated chat); then the caption must
        tokenize to ``/attach`` (a missing caption gets the usage text,
        a caption of another command the unknown hint); project/task
        resolution runs through the command path's own helpers
        (``_split_project`` + ``_resolve_task`` — the disambiguation
        list and the not-found texts pass through unchanged, and no
        failure path downloads anything); the filename/content type come
        from the file's declared metadata (document: ``file_name``/
        ``mime_type`` with an ``"attachment"`` /
        ``"application/octet-stream"`` fallback, photo:
        ``photo.jpg`` / ``image/jpeg``); the pre-checks run before any
        download (the declared type must be in
        ``Store.ALLOWED_ATTACHMENT_TYPES``, a known declared size must be
        within ``Store.MAX_ATTACHMENT_SIZE``); then the file is
        downloaded (``api.get_file_bytes``) and stored
        (``store.add_attachment`` — which sanitizes the filename and
        re-enforces the type allowlist and the size cap as the backstop
        when Telegram's metadata was missing). The success reply is a
        :class:`KeyboardReply` confirmation with the task's detail
        button and the Main-menu row (the ``/add`` confirmation's
        shape); every failure path is a plain ``str``.
        """
        if auth is not None and not auth.is_authenticated(chat_id):
            return AUTH_REQUIRED_TEXT
        if api is None or file.file_id is None:
            return ATTACH_ERROR_TEXT
        cmd = _command_token(file.caption)
        if cmd != "/attach":
            return (
                ATTACH_USAGE_TEXT if cmd is None else UNKNOWN_HINT
            )
        words = file.caption.split()[1:]
        if not words:
            return ATTACH_USAGE_TEXT
        project, rest = _split_project(store, words)
        if project is None:
            return (
                f"Project '{words[0]}' not found. "
                "Use /projects to list projects."
            )
        if not rest:
            return ATTACH_USAGE_TEXT
        task = _resolve_task(store, project, " ".join(rest))
        if not isinstance(task, dict):
            return task
        if file.is_photo:
            filename = file.filename or "photo.jpg"
            content_type = file.content_type or "image/jpeg"
        else:
            filename = file.filename or "attachment"
            content_type = file.content_type or "application/octet-stream"
        if content_type not in Store.ALLOWED_ATTACHMENT_TYPES:
            return (
                f"File type '{content_type}' is not allowed: "
                "markdown/plain text and png, jpeg, gif, webp or svg "
                "images only."
            )
        if file.size is not None and file.size > Store.MAX_ATTACHMENT_SIZE:
            return (
                f"File is too large ({_human_size(file.size)}); "
                "the limit is 10 MB."
            )
        try:
            data = await api.get_file_bytes(file.file_id)
        except BotAPIError:
            return ATTACH_ERROR_TEXT
        try:
            meta = store.add_attachment(
                project["id"], task["number"], filename, content_type, data
            )
        except Exception:
            return ATTACH_ERROR_TEXT
        text = (
            f"Attached '{meta['filename']}' to #{task['number']} "
            f"'{task['title']}' ({_human_size(meta['size'])})."
        )
        button = {
            "text": _truncate_button_label(f"#{task['number']} {task['title']}"),
            "callback_data": f"t:{project['id']}:{task['number']}",
        }
        return KeyboardReply(
            text, {"inline_keyboard": [[button], [_main_menu_button()]]}
        )

    def dispatch(
        text: Optional[str],
        chat_id: Optional[int] = None,
        file: Optional[IncomingFile] = None,
    ) -> Optional[Union[Reply, Awaitable[Reply]]]:
        # A file message is answered by the async file handler (the
        # /attach flow): dispatch returns its coroutine, which run_bot
        # awaits (inspect.iscoroutine). Text messages keep the sync path.
        if file is not None:
            return _dispatch_file(file, chat_id)
        cmd = _command_token(text)
        if cmd == "/login":
            if chat_id is None:
                return None  # no sender to authenticate
            args = _arg_words(text)
            if not args:
                return LOGIN_USAGE_TEXT
            # The password is everything after the command token, so a
            # password may contain (single) spaces. A store failure (locked
            # or corrupted DB) answers a plain failure, like the other
            # store-backed commands.
            try:
                ok = (
                    auth.authenticate(chat_id, " ".join(args))
                    if auth is not None
                    else True
                )
            except Exception:
                ok = False
            return LOGIN_OK_TEXT if ok else LOGIN_FAIL_TEXT
        if cmd == "/whoami":
            if chat_id is None:
                return None  # no sender whose id could be shown
            return WHOAMI_TEXT.format(chat_id=chat_id)
        if cmd in COMMAND_TABLE and not _authed(chat_id):
            return AUTH_REQUIRED_TEXT
        entry = COMMAND_TABLE.get(cmd)
        if entry is not None:
            handler, error_text = entry
            try:
                return handler(store, text, chat_id)
            except Exception:
                return error_text
        return reply_for(text)

    return dispatch


def _project_not_found(project_id: int) -> str:
    """The not-found reply for an unknown project id (all families)."""
    return (
        f"Project '{project_id}' not found. "
        "Use /projects to list projects."
    )


def _task_not_found(project_name: str, number: int) -> str:
    """The not-found reply for an unknown task number in ``project_name``."""
    return f"Task #{number} not found in {project_name}."


def _attachment_not_found(
    project_name: str, number: int, attachment_id: int
) -> str:
    """The not-found reply for an unknown attachment on a task."""
    return (
        f"Attachment {attachment_id} not found on task "
        f"#{number} ({project_name}). Use /task "
        f"{project_name} {number} to list the task's "
        "attachments."
    )


def _resolve_project_id(
    store: Store, project_id: int, error_text: str
) -> Union[dict, CallbackAction]:
    """Resolve a project by id, or a ready-to-return
    :class:`CallbackAction`.

    ``NotFound`` answers with the project's not-found text; any other
    store failure answers with ``error_text`` (the family's error text).
    The id-based twin of :func:`_resolve_project` (which resolves text
    references for the command path).
    """
    try:
        return store.get_project(project_id)
    except NotFound:
        return CallbackAction(reply=_project_not_found(project_id))
    except Exception:
        return CallbackAction(reply=error_text)


def _resolve_task_number(
    store: Store, project: dict, project_id: int, number: int, error_text: str
) -> Union[dict, CallbackAction]:
    """Resolve a task by number in ``project``, or a ready-to-return
    :class:`CallbackAction`.

    ``NotFound`` answers with the task's not-found text (named after
    ``project``); any other store failure answers with ``error_text``.
    The id-based twin of :func:`_resolve_task` (which resolves text
    references for the command path).
    """
    try:
        return store.get_task(project_id, number)
    except NotFound:
        return CallbackAction(
            reply=_task_not_found(project["name"], number)
        )
    except Exception:
        return CallbackAction(reply=error_text)


def _resolve_move_context(
    store: Store, project_id: int, number: int
) -> Union[CallbackAction, tuple[dict, dict]]:
    """Resolve project + task for the ``m:``/``c:``/``x:`` move families.

    The same resolution as the ``t:`` family, including its not-found
    texts; any other store failure answers with :data:`MOVE_ERROR_TEXT`.
    Returns ``(project, task)`` or a ready-to-return
    :class:`CallbackAction`.
    """
    project = _resolve_project_id(store, project_id, MOVE_ERROR_TEXT)
    if not isinstance(project, dict):
        return project
    task = _resolve_task_number(
        store, project, project_id, number, MOVE_ERROR_TEXT
    )
    if not isinstance(task, dict):
        return task
    return project, task


def _message_is_rich(callback_query: dict) -> bool:
    """Whether the callback's original message is a Rich Message.

    Bot API 10.1 added ``Message.rich_message`` ("Message is a rich
    formatted message"); a rich message's ``text`` is empty or absent.
    Presence check only — the ``m:``/``c:``/``x:`` handlers re-render the
    view from the store and never need the received content (which is a
    parsed block tree, not markdown — the reason the ``s:``/``u:``
    toggle's in-place echo is #106).
    """
    return isinstance(
        (callback_query.get("message") or {}).get("rich_message"), dict
    )


def _detail_edit(
    store: Store, project: dict, number: int, callback_query: dict
) -> Union[CallbackAction, MessageEdit, RichMessageEdit]:
    """The in-place edit back to the task's fresh detail view.

    Re-renders :func:`format_task_view` after an ``m:``/``c:``/``x:``
    action (the new state text, the keyboard now excluding the new current
    state); the chat id and subscription state come from the callback's
    message (an inaccessible message, with no chat, gets the plain text).
    A store failure answers :data:`MOVE_ERROR_TEXT` instead.

    The re-render is a :class:`RichReply` (the view's rich form): a rich
    original message (a ``rich_message`` field on the callback's message)
    gets a :class:`RichMessageEdit` — a text edit of a rich message fails
    server-side — while a plain original keeps today's plain
    :class:`MessageEdit` with the markdown on the ``text`` field (the
    kill switch off, or the send chain degraded the original to
    HTML/plain).
    """
    project_id = project["id"]
    try:
        task = store.get_task(project_id, number)
        history = store.get_history(project_id, number)
    except Exception:
        return CallbackAction(reply=MOVE_ERROR_TEXT)
    chat_id = ((callback_query.get("message") or {}).get("chat") or {}).get(
        "id"
    )
    subscribed = False
    if chat_id is not None:
        try:
            subscribed = any(
                s["project_id"] == project_id
                for s in store.list_subscriptions(chat_id)
            )
        except Exception:
            return CallbackAction(reply=MOVE_ERROR_TEXT)
    reply = format_task_view(task, project, history, chat_id, subscribed)
    if isinstance(reply, str):
        return MessageEdit(reply)
    if isinstance(reply, RichReply):
        if _message_is_rich(callback_query):
            # A rich original: the edit rides the rich payload (a text
            # edit of a rich message 400s server-side).
            return RichMessageEdit(reply.markdown, reply.reply_markup)
        # A plain original (kill switch off, or the send chain degraded
        # the original): today's behavior, markdown on a plain text edit.
        return MessageEdit(reply.markdown, reply.reply_markup)
    return MessageEdit(reply.text, reply.reply_markup)


def make_callback_dispatch(
    store: Store,
    auth: Optional[Auth] = None,
) -> Callable[[dict], Optional[CallbackAction]]:
    """Build the callback_query→action dispatcher for a bot bound to ``store``.

    The inline-keyboard pipeline's dispatch layer, mirroring
    :func:`make_dispatch`: the raw ``callback_query`` update dict is passed
    through (fields ``id``, ``data``, ``message``, ``from``,
    ``chat_instance``) and the factory returns a
    :class:`CallbackAction` (or None for "nothing to do" — answered with
    the out-of-date toast by ``run_bot``).

    Structurally the dispatch is a table keyed by the payload's
    ``(prefix, arity)`` shape — one entry per family below (the ``h``
    prefix owns two arities, the bare hub payload and the ``h:<route>``
    menu routes) — and each entry is one small handler that encapsulates
    its family's field validation, resolution order and not-found/error
    texts. The repeated resolve-else-not-found skeleton lives in
    :func:`_resolve_project_id`/:func:`_resolve_task_number`; adding a
    family is one handler plus one table entry.

    There are eight payload families. ``p:<project-id>`` (the per-project
    buttons of the ``/projects`` view) opens that project's task list view
    — :func:`tasks_view` resolved by id, the same view the user would get
    typing ``/tasks <id>`` — as a new message.
    ``t:<project-id>:<number>`` (the per-task buttons of the ``/tasks``
    view) opens that task's detail view — ``get_task`` + ``get_history`` +
    ``format_task_view`` — as a new message carrying the task view's own
    buttons: the chat id is read from the callback's message, so the
    detail's keyboard reflects that chat's subscription state (an
    inaccessible message, with no chat, gets the plain text instead).
    ``a:<project-id>:<number>:<attachment-id>`` (the per-attachment buttons
    of the ``/task`` view) shows the attachment in the button's chat: small
    markdown (<16 KB) inline as a Rich Message, small plain text inline as
    a plain message, images as a photo, other content as a file — the same
    resolution as ``/attachment`` (:func:`attachment_reply`). ``s:<project-id>``/``u:<project-id>`` (the
    subscribe/unsubscribe toggle of the ``/task`` view) toggles the
    button's chat's subscription in the store and re-renders the message in
    place (``editMessageText``): the text is unchanged, the pressed toggle
    button flips to its other state, and the other rows (the attachment
    buttons) are preserved — a stale keyboard with no toggle falls back to
    a single toggle row; a toast confirms the new state.
    ``m:<project-id>:<number>:<state-index>`` (the per-state buttons of the
    ``/task`` view) moves the task to the ``db.WORKFLOW_STATES[state-index]``
    workflow state: a single-task move is applied and the message re-renders
    in place to the fresh detail view (toast ``Moved #<n> to <state>.``),
    while a move that would pull prerequisites along answers with the
    confirm/cancel keyboard (:func:`_confirm_move_markup`) in place and
    writes nothing. A rich original message (a ``rich_message`` field on
    the callback's message) is re-rendered in place with the rich
    ``editMessageText`` payload (:class:`RichMessageEdit`) — a text edit of
    a rich message fails server-side — while a plain original keeps the
    plain edit; ``run_bot`` re-sends the content as a fresh message when
    a rich edit fails. ``c:<project-id>:<number>:<state-index>`` confirms
    such a cascade (applies it with ``confirm=True``, re-renders the
    detail in place); ``x:<project-id>:<number>`` cancels it (re-renders
    the plain detail, no store change).
    ``h`` (the main-menu hub's own button) opens the menu view
    (:func:`menu_view`) and the ``h:<route>`` payloads open the menu's
    targets as new messages: ``h:p`` the project list (:func:`project_view`),
    ``h:t`` the all-projects task list (:func:`tasks_view` with no
    argument), ``h:s`` the button's chat's subscription list
    (:func:`subscribe_view` with no argument — an inaccessible message,
    with no chat, returns the out-of-date toast), ``h:a`` the add-task
    usage text and ``h:h`` the help text. The ``p``/``t``/``s`` routes are
    store-backed, so a non-NotFound store failure answers
    :data:`PROJECTS_ERROR_TEXT`/:data:`TASKS_ERROR_TEXT`/
    :data:`SUBSCRIBE_ERROR_TEXT`; the home, add-task and help routes are
    static.
    All eight families are strictly shaped payloads
    (the right prefix and arity — ``h`` alone or ``h:<route>`` with the
    route among ``p``/``t``/``s``/``a``/``h``, integer ``:``-separated
    fields where ids appear, and — for the state index — an in-range
    value); an unknown project, task or
    attachment gets an informative text reply (the same wording as the
    corresponding command's not-found reply); any other shape returns None
    for the out-of-date toast. A non-NotFound store failure in any family
    (a locked or corrupted DB) yields the family's error-text reply — the
    same convention as :func:`make_dispatch` — instead of propagating out
    of :func:`run_bot`'s per-update handler (which catches only
    BotAPIError) and killing the long-poll process.

    Board access is gated by ``auth`` (an :class:`Auth`; the production
    bot always passes one): the callback's chat id (from the original
    message's ``chat.id``) is resolved up front, and a missing or
    unauthenticated chat gets ``AUTH_REQUIRED_TEXT`` as both toast and
    reply for every payload family — in particular no attachment bytes are
    ever sent to an unauthenticated chat. Without an ``auth`` the presses
    are open (the legacy, unauthenticated behavior).
    """

    def _callback_chat_id(callback_query: dict) -> Optional[int]:
        """The chat the button was pressed in (the original message's
        ``chat.id``), or None for an inaccessible message (no chat)."""
        return ((callback_query.get("message") or {}).get("chat") or {}).get(
            "id"
        )

    def _handle_p(
        parts: list[str], callback_query: dict
    ) -> Optional[CallbackAction]:
        # The /projects view's per-project buttons: that project's task
        # list view.
        if not parts[1].isdigit():
            return None  # malformed → run_bot's out-of-date toast
        project_id = int(parts[1])
        resolved = _resolve_project_id(store, project_id, TASKS_ERROR_TEXT)
        if not isinstance(resolved, dict):
            return resolved
        # The same view typing "/tasks <id>" would send (including its
        # own t: keyboard when the project has active tasks).
        try:
            view = tasks_view(store, str(project_id))
        except Exception:
            return CallbackAction(reply=TASKS_ERROR_TEXT)
        return CallbackAction(reply=view)

    def _handle_t(
        parts: list[str], callback_query: dict
    ) -> Optional[CallbackAction]:
        # The /tasks view's per-task buttons: the task's detail view.
        # (The fields parse with int(), not isdigit(): a minus sign is
        # malformed for the other families but resolves here to the
        # project's not-found reply.)
        try:
            project_id = int(parts[1])
            number = int(parts[2])
        except ValueError:
            return None
        project = _resolve_project_id(store, project_id, TASK_ERROR_TEXT)
        if not isinstance(project, dict):
            return project
        task = _resolve_task_number(
            store, project, project_id, number, TASK_ERROR_TEXT
        )
        if not isinstance(task, dict):
            return task
        try:
            history = store.get_history(project_id, number)
        except Exception:
            return CallbackAction(reply=TASK_ERROR_TEXT)
        # The detail reply carries its own keyboard only when the
        # button's chat is known (an inaccessible message arrives with
        # no chat → the plain text, as before): the toggle button
        # reflects that chat's subscription state.
        chat_id = _callback_chat_id(callback_query)
        subscribed = False
        if chat_id is not None:
            try:
                subscribed = any(
                    s["project_id"] == project_id
                    for s in store.list_subscriptions(chat_id)
                )
            except Exception:
                return CallbackAction(reply=TASK_ERROR_TEXT)
        return CallbackAction(
            reply=format_task_view(task, project, history, chat_id, subscribed)
        )

    def _handle_a(
        parts: list[str], callback_query: dict
    ) -> Optional[CallbackAction]:
        # The /task view's per-attachment buttons: the attachment itself.
        if not (
            parts[1].isdigit() and parts[2].isdigit() and parts[3].isdigit()
        ):
            return None  # malformed → run_bot's out-of-date toast
        project_id = int(parts[1])
        number = int(parts[2])
        attachment_id = int(parts[3])
        project = _resolve_project_id(
            store, project_id, ATTACHMENT_ERROR_TEXT
        )
        if not isinstance(project, dict):
            return project
        task = _resolve_task_number(
            store, project, project_id, number, ATTACHMENT_ERROR_TEXT
        )
        if not isinstance(task, dict):
            return task
        try:
            meta, data = store.get_task_attachment(
                project_id, number, attachment_id
            )
        except NotFound:
            return CallbackAction(
                reply=_attachment_not_found(
                    project["name"], number, attachment_id
                )
            )
        except Exception:
            return CallbackAction(reply=ATTACHMENT_ERROR_TEXT)
        return CallbackAction(reply=attachment_reply(meta, data, task))

    def _handle_toggle(
        parts: list[str], callback_query: dict
    ) -> Optional[CallbackAction]:
        # The /task view's subscribe/unsubscribe toggle (s:/u:): flips
        # the button's chat's subscription, re-renders the message in
        # place.
        if not parts[1].isdigit():
            return None  # malformed → run_bot's out-of-date toast
        project_id = int(parts[1])
        # The family's error text, per prefix (a failed press of the
        # subscribe toggle reports a subscribe failure, etc.).
        error_text = (
            SUBSCRIBE_ERROR_TEXT if parts[0] == "s" else UNSUBSCRIBE_ERROR_TEXT
        )
        project = _resolve_project_id(store, project_id, error_text)
        if not isinstance(project, dict):
            return project
        # A subscription is per chat: an inaccessible message (no chat)
        # cannot be toggled → the out-of-date toast.
        chat_id = _callback_chat_id(callback_query)
        if chat_id is None:
            return None
        if parts[0] == "s":
            try:
                store.subscribe_project(chat_id, project_id)
            except Exception:
                return CallbackAction(reply=error_text)
            subscribed = True
            answer = f"Subscribed to {project['name']}"
        else:
            try:
                store.unsubscribe_project(chat_id, project_id)
            except Exception:
                return CallbackAction(reply=error_text)
            subscribed = False
            answer = f"Unsubscribed from {project['name']}"
        # In-place re-render: a toggle leaves the message text unchanged
        # and only flips the pressed toggle button; an inaccessible
        # message (no text) gets the toast only. A Rich Message original
        # also carries no ``text`` (its content is a parsed block tree,
        # Bot API 10.1), so on a rich view the press bails to the toast
        # only and the pressed button stays stale — echoing the rich
        # content with the flipped keyboard is #106.
        message = callback_query.get("message") or {}
        text = message.get("text")
        if text is None:
            return CallbackAction(answer_text=answer)
        rows = (message.get("reply_markup") or {}).get("inline_keyboard") or []
        return CallbackAction(
            answer_text=answer,
            edit=MessageEdit(
                text,
                {
                    "inline_keyboard": _flip_toggle(
                        rows, project_id, callback_query["data"], subscribed
                    )
                },
            ),
        )

    def _handle_move(
        parts: list[str], callback_query: dict
    ) -> Optional[CallbackAction]:
        # The /task view's per-state buttons (m:) and the confirm button
        # (c:).
        if not all(parts[i].isdigit() for i in (1, 2, 3)):
            return None  # malformed → run_bot's out-of-date toast
        # The state index is the payload's last field; out of range →
        # malformed → the out-of-date toast.
        if int(parts[3]) >= len(db.WORKFLOW_STATES):
            return None
        project_id, number = int(parts[1]), int(parts[2])
        target = db.WORKFLOW_STATES[int(parts[3])]
        resolved = _resolve_move_context(store, project_id, number)
        if not isinstance(resolved, tuple):
            return resolved
        project, task = resolved
        # Archived details never carry state buttons (or confirms).
        if task["state"] == db.ARCHIVED_STATE:
            return None
        if task["state"] == target:
            # m: a stale button; c: someone moved it in the meantime.
            # A toast only — no store call, no edit.
            return CallbackAction(answer_text=f"Already in {target}.")
        try:
            affected = store.plan_move(project_id, number, target)
        except Exception:
            return CallbackAction(reply=MOVE_ERROR_TEXT)
        if parts[0] == "m" and len(affected) > 1:
            # The move would pull prerequisites along: edit the message
            # to the confirm keyboard, nothing is written. The prompt is
            # plain text, but a rich original still needs the rich
            # payload (a text edit of a rich message 400s server-side).
            confirm = _confirm_move_markup(project_id, task, target, affected)
            if _message_is_rich(callback_query):
                return CallbackAction(
                    edit=RichMessageEdit(confirm.text, confirm.reply_markup)
                )
            return CallbackAction(
                edit=MessageEdit(confirm.text, confirm.reply_markup)
            )
        try:
            store.move_task(
                project_id, number, target,
                confirm=(parts[0] == "c"),
            )
        except Exception:
            return CallbackAction(reply=MOVE_ERROR_TEXT)
        detail = _detail_edit(store, project, number, callback_query)
        if not isinstance(detail, (MessageEdit, RichMessageEdit)):
            return detail
        if len(affected) == 1:
            answer = f"Moved #{number} to {target}."
        else:
            answer = f"Moved {len(affected)} tasks to {target}."
        return CallbackAction(answer_text=answer, edit=detail)

    def _handle_cancel(
        parts: list[str], callback_query: dict
    ) -> Optional[CallbackAction]:
        # The confirm keyboard's Cancel: re-render the plain detail,
        # no store change.
        if not all(parts[i].isdigit() for i in (1, 2)):
            return None  # malformed → run_bot's out-of-date toast
        project_id, number = int(parts[1]), int(parts[2])
        resolved = _resolve_move_context(store, project_id, number)
        if not isinstance(resolved, tuple):
            return resolved
        project, task = resolved
        detail = _detail_edit(store, project, number, callback_query)
        if not isinstance(detail, (MessageEdit, RichMessageEdit)):
            return detail
        return CallbackAction(answer_text="Cancelled.", edit=detail)

    def _handle_home(
        parts: list[str], callback_query: dict
    ) -> Optional[CallbackAction]:
        # The main-menu hub's own button (what the other views'
        # Main-menu rows will send): opens the menu view, static.
        return CallbackAction(reply=menu_view())

    def _handle_route(
        parts: list[str], callback_query: dict
    ) -> Optional[CallbackAction]:
        # The menu's route buttons (#61 sends the same payloads). The
        # store-backed routes report a non-NotFound store failure with
        # the board family's error text; the help route is static and
        # cannot fail.
        route = parts[1]
        if route not in ("p", "t", "s", "a", "h"):
            return None  # unknown route → run_bot's out-of-date toast
        if route == "p":
            try:
                return CallbackAction(reply=project_view(store))
            except Exception:
                return CallbackAction(reply=PROJECTS_ERROR_TEXT)
        if route == "t":
            try:
                return CallbackAction(reply=tasks_view(store))
            except Exception:
                return CallbackAction(reply=TASKS_ERROR_TEXT)
        if route == "s":
            # The subscription list is per chat: an inaccessible
            # message (no chat) → the out-of-date toast.
            chat_id = _callback_chat_id(callback_query)
            if chat_id is None:
                return None
            try:
                return CallbackAction(reply=subscribe_view(store, chat_id))
            except Exception:
                return CallbackAction(reply=SUBSCRIBE_ERROR_TEXT)
        if route == "a":
            # The /add usage now lists the board's task types, so it is
            # a store read like the other board routes.
            try:
                return CallbackAction(reply=add_usage_text(store))
            except Exception:
                return CallbackAction(reply=TASKS_ERROR_TEXT)
        return CallbackAction(reply=HELP_TEXT)

    # The dispatch table: (payload prefix, arity) → the family's handler.
    # The h prefix owns two arities — the bare hub payload and the
    # h:<route> menu routes.
    handlers = {
        ("p", 2): _handle_p,
        ("t", 3): _handle_t,
        ("a", 4): _handle_a,
        ("s", 2): _handle_toggle,
        ("u", 2): _handle_toggle,
        ("m", 4): _handle_move,
        ("c", 4): _handle_move,
        ("x", 3): _handle_cancel,
        ("h", 1): _handle_home,
        ("h", 2): _handle_route,
    }

    def callback_dispatch(callback_query: dict) -> Optional[CallbackAction]:
        data = callback_query.get("data")
        if not isinstance(data, str):
            return None
        # Board access: the chat the button was pressed in (the original
        # message's chat.id) must hold a login session — persisted in the
        # store, so a bot restart does not log it out.
        if auth is not None:
            if not auth.is_authenticated(_callback_chat_id(callback_query)):
                return CallbackAction(
                    answer_text=AUTH_REQUIRED_TEXT,
                    reply=AUTH_REQUIRED_TEXT,
                )
        parts = data.split(":")
        handler = handlers.get((parts[0], len(parts)))
        # Any other shape (unknown family or arity; each handler also
        # rejects its own malformed fields): nothing to do → run_bot's
        # out-of-date toast.
        return handler(parts, callback_query) if handler is not None else None

    return callback_dispatch


class BotAPI:
    """Minimal Telegram Bot API client: getMe, getUpdates, sendMessage,
    sendRichMessage, sendDocument, sendPhoto, answerCallbackQuery,
    editMessageText, getFile.

    Accepts an ``httpx.AsyncClient`` for tests (e.g. with
    ``httpx.MockTransport``); production uses a default client that
    ``aclose`` closes.
    """

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._client = client if client is not None else httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        self._owns_client = client is None

    def _redact(self, message: str) -> str:
        """Strip the bot token from a message before it reaches logs.

        The request URLs embed the token, and httpx error messages can
        echo the request URL (behavior varies between versions), so any
        message that will be printed must be scrubbed here.
        """
        if self._token:
            message = message.replace(self._token, "[REDACTED]")
        return message

    async def _call(self, method: str, **params: Any) -> Any:
        url = f"{self._base_url}/bot{self._token}/{method}"
        try:
            response = await self._post(url, json=params)
        except httpx.HTTPError as exc:
            raise BotAPIError(self._redact(f"telegram request failed: {exc}")) from exc
        return self._parse(response)

    async def _call_multipart(
        self,
        method: str,
        fields: dict,
        file_field: str,
        filename: str,
        data: bytes,
        content_type: str,
    ) -> Any:
        url = f"{self._base_url}/bot{self._token}/{method}"
        files = {file_field: (filename, data, content_type)}
        try:
            response = await self._post(url, fields=fields, files=files)
        except httpx.HTTPError as exc:
            raise BotAPIError(self._redact(f"telegram request failed: {exc}")) from exc
        return self._parse(response)

    @staticmethod
    def _parse(response: httpx.Response) -> Any:
        """Extract ``result`` from a Bot API response body.

        Raises :class:`BotAPIError` on a non-JSON body or an ``ok:false``
        payload (with the API error code when present).
        """
        try:
            data = response.json()
        except ValueError as exc:
            raise BotAPIError(
                f"telegram returned a non-JSON response (HTTP {response.status_code})"
            ) from exc
        if not data.get("ok", False):
            raise BotAPIError(
                data.get("description") or f"HTTP {response.status_code}",
                data.get("error_code"),
            )
        return data.get("result")

    async def _post(
        self,
        url: str,
        json: Optional[dict] = None,
        fields: Optional[dict] = None,
        files: Optional[dict] = None,
    ) -> httpx.Response:
        response = await self._client.post(url, json=json, data=fields, files=files)
        if response.status_code == 429:
            # Rate limited: back off as requested (capped) and retry once.
            await asyncio.sleep(self._retry_after(response))
            response = await self._client.post(url, json=json, data=fields, files=files)
        return response

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        try:
            seconds = float(response.json().get("parameters", {}).get("retry_after", 5))
        except (ValueError, TypeError, AttributeError):
            seconds = 5.0
        return min(max(seconds, 0.0), MAX_RETRY_AFTER)

    async def get_me(self) -> dict:
        result = await self._call("getMe")
        return result if isinstance(result, dict) else {}

    async def get_updates(
        self, offset: Optional[int] = None, timeout: int = POLL_TIMEOUT
    ) -> list[dict]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = offset
        result = await self._call("getUpdates", **params)
        return list(result) if result else []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Optional[ReplyMarkup] = None,
        parse_mode: Optional[str] = None,
    ) -> dict:
        """Send a text message; ``parse_mode`` (e.g. ``"HTML"``) joins the
        params only when truthy, so a plain send stays byte-identical to
        before (the rich fallback's HTML leg passes ``"HTML"``)."""
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup:
            params["reply_markup"] = reply_markup
        result = await self._call("sendMessage", **params)
        return result if isinstance(result, dict) else {}

    async def send_rich_message(
        self,
        chat_id: int,
        markdown: str,
        reply_markup: Optional[ReplyMarkup] = None,
    ) -> dict:
        """Send a Rich Message (Bot API 10.1+): ``markdown`` is
        GFM-compatible; ``reply_markup`` threads an inline keyboard.

        An ``ok:false`` 400 (unparseable markdown) or 404 (method unknown on
        an old local Bot API server) surfaces as ``BotAPIError`` with the
        ``error_code`` set — callers use it to fall back.
        """
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": {"markdown": markdown},
        }
        if reply_markup:
            params["reply_markup"] = reply_markup
        result = await self._call("sendRichMessage", **params)
        return result if isinstance(result, dict) else {}

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        data: bytes,
        content_type: str,
        caption: Optional[str] = None,
        reply_markup: Optional[ReplyMarkup] = None,
    ) -> dict:
        fields: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            fields["caption"] = caption
        if reply_markup:
            # Multipart form fields are strings: the Bot API accepts the
            # keyboard as a JSON string on file uploads.
            fields["reply_markup"] = json.dumps(reply_markup)
        result = await self._call_multipart(
            "sendDocument", fields, "document", filename, data, content_type
        )
        return result if isinstance(result, dict) else {}

    async def send_photo(
        self,
        chat_id: int,
        filename: str,
        data: bytes,
        content_type: str,
        caption: Optional[str] = None,
        reply_markup: Optional[ReplyMarkup] = None,
    ) -> dict:
        fields: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            fields["caption"] = caption
        if reply_markup:
            # Multipart form fields are strings: the Bot API accepts the
            # keyboard as a JSON string on file uploads.
            fields["reply_markup"] = json.dumps(reply_markup)
        result = await self._call_multipart(
            "sendPhoto", fields, "photo", filename, data, content_type
        )
        return result if isinstance(result, dict) else {}

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
        cache_time: Optional[int] = None,
    ) -> dict:
        """Answer a callback so the client's progress bar stops spinning.

        ``text`` is the toast shown under the button, ``show_alert``
        upgrades it to an alert dialog, ``cache_time`` the client-side
        answer cache TTL (seconds).
        """
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text
        if show_alert:
            params["show_alert"] = show_alert
        if cache_time:
            params["cache_time"] = cache_time
        result = await self._call("answerCallbackQuery", **params)
        return result if isinstance(result, dict) else {}

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: Optional[str] = None,
        reply_markup: Optional[ReplyMarkup] = None,
        rich_markdown: Optional[str] = None,
    ) -> dict:
        """Update a message's text (and inline keyboard) in place.

        Exactly one payload is required: the plain ``text``, or
        ``rich_markdown`` (a Rich Message edit, Bot API 10.1+). A text edit
        of a rich message fails server-side, so rich messages must be edited
        with ``rich_markdown``.
        """
        if (text is None) == (rich_markdown is None):
            raise ValueError(
                "exactly one of text / rich_markdown must be given"
            )
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
        }
        if rich_markdown is not None:
            params["rich_message"] = {"markdown": rich_markdown}
        else:
            params["text"] = text
        if reply_markup:
            params["reply_markup"] = reply_markup
        result = await self._call("editMessageText", **params)
        return result if isinstance(result, dict) else {}

    async def set_my_commands(
        self,
        commands: list[dict],
        scope: Optional[dict] = None,
        language_code: Optional[str] = None,
    ) -> bool:
        """Replace the bot's command menu with ``commands`` (full overwrite).

        Full overwrite: any command not listed here is removed. Idempotent and
        safe to call at every startup. ``scope`` narrows the change to a
        specific ``BotCommandScope``; ``language_code`` to one language.
        ``commands`` is a list of ``{"command": ..., "description": ...}`` dicts.
        """
        params: dict[str, Any] = {"commands": list(commands)}
        if scope is not None:
            params["scope"] = scope
        if language_code:
            params["language_code"] = language_code
        result = await self._call("setMyCommands", **params)
        return bool(result)

    async def get_my_commands(
        self,
        scope: Optional[dict] = None,
        language_code: Optional[str] = None,
    ) -> list[dict]:
        """Return the bot's command menu as a list of ``{command, description}``.

        ``scope`` / ``language_code`` narrow the query; both optional.
        """
        params: dict[str, Any] = {}
        if scope is not None:
            params["scope"] = scope
        if language_code:
            params["language_code"] = language_code
        result = await self._call("getMyCommands", **params)
        return list(result) if result else []

    async def delete_my_commands(
        self,
        scope: Optional[dict] = None,
        language_code: Optional[str] = None,
    ) -> bool:
        """Remove the bot's command menu.

        Uses the dedicated ``deleteMyCommands`` endpoint (not the empty-array
        ``setMyCommands`` trick): it is the documented method and its
        ``scope`` / ``language_code`` arguments match this signature exactly.
        """
        params: dict[str, Any] = {}
        if scope is not None:
            params["scope"] = scope
        if language_code:
            params["language_code"] = language_code
        result = await self._call("deleteMyCommands", **params)
        return bool(result)

    async def get_file(self, file_id: str) -> dict:
        """The metadata of a Bot API file (``getFile``): ``file_id``,
        ``file_unique_id``, ``file_size``, ``file_path`` (the URL suffix
        used by :meth:`download_file`)."""
        result = await self._call("getFile", file_id=file_id)
        return result if isinstance(result, dict) else {}

    async def download_file(self, file_path: str) -> bytes:
        """Download a Bot API file's bytes (a plain GET — no JSON body —
        to ``<base>/file/bot<token>/<file_path>``).

        A transport failure or a non-200 answer raises
        :class:`BotAPIError` (the caller turns it into the family's
        error text; the poll loop never crashes on a download).
        """
        url = f"{self._base_url}/file/bot{self._token}/{file_path}"
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise BotAPIError(self._redact(f"telegram request failed: {exc}")) from exc
        if response.status_code != 200:
            raise BotAPIError(
                f"telegram file download failed (HTTP {response.status_code})"
            )
        return response.content

    async def get_file_bytes(self, file_id: str) -> bytes:
        """A Bot API file's bytes: :meth:`get_file` for the ``file_path``,
        then :meth:`download_file` for the content."""
        file = await self.get_file(file_id)
        file_path = file.get("file_path")
        if not file_path:
            raise BotAPIError("telegram returned no file_path for the file")
        return await self.download_file(file_path)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


async def register_my_commands(api, commands=None) -> None:
    """Post the command menu via setMyCommands to all private chats.

    Failure is logged, not raised: this runs at startup when Telegram may be
    unreachable, and setMyCommands is a full overwrite (idempotent), so a
    failed call must never abort polling — a later start retried it.
    """
    try:
        await api.set_my_commands(
            build_my_commands(commands if commands is not None else COMMAND_REGISTRY),
            scope=MENU_SCOPE,
        )
    except BotAPIError as exc:
        print(f"yask: telegram command menu not set ({exc})", file=sys.stderr)


class Notifier:
    """Push task state-change notifications to subscribed chats.

    The cursor sits after the newest fully-notified ``state_history`` row.
    :meth:`seed` pins it to the current maximum at startup, so only changes
    made while the bot runs are notified — no replay storm on restart.
    :meth:`check` is the per-poll-cycle hook: it fetches the changes
    recorded since the cursor and, in id order, sends each one to every
    chat subscribed to its project. Each notification carries one inline
    button (``t:<project-id>:<number>``) that opens the task's detail view.
    The cursor advances only past a change that reached all of its
    subscribers, so a failed send is retried on the next cycle (chats that
    already received it get a duplicate — accepted, a lost change would be
    worse).

    Notifications leak task titles and states, so when an :class:`Auth` is
    passed, each change fans out only to subscribed chats that are
    *currently* authenticated; a subscriber that has logged out misses
    changes made while logged out (the cursor still advances — a lost
    change would be a leak, a replay a surprise).
    """

    def __init__(
        self,
        api: BotAPI,
        store: Store,
        auth: Optional[Auth] = None,
        rich: bool = True,
    ) -> None:
        self._api = api
        self._store = store
        self._auth = auth
        self._rich = rich
        self._cursor: int = 0

    def seed(self) -> None:
        """Pin the cursor to the current history maximum (bot startup)."""
        self._cursor = self._store.max_state_history_id()

    async def check(self) -> None:
        """Send the pending state changes to every subscribed chat."""
        changes = self._store.new_state_changes(self._cursor)
        for change in changes:
            for chat_id in self._store.subscribed_chats(change["project_id"]):
                if self._auth is not None and not self._auth.is_authenticated(chat_id):
                    continue  # no board data for unauthenticated chats
                await _send_reply(
                    self._api, chat_id, format_notification(change), self._rich
                )
            self._cursor = change["id"]


async def _send_rich_with_fallback(
    api: BotAPI, chat_id: int, reply: RichReply, rich: bool
) -> None:
    """Send a :class:`RichReply` through the rich → HTML → plain chain.

    1. Rich mode on: ``sendRichMessage`` (the markdown plus, when set, the
       keyboard).
    2. Any rich-leg failure (400 unparseable markdown, 404 unknown method
       on an old local Bot API server, or any transport failure) →
       ``sendMessage`` with ``parse_mode=HTML`` on
       :func:`markdown_to_html` of the markdown.
    3. Any HTML-leg failure (e.g. 400) → plain text — exactly today's
       behavior.

    Each leg fires only if the previous one raised, so a failure never
    double-sends and the worst case is exactly today's plain output. The
    markdown source — not the converted HTML — is re-truncated to
    :data:`REGULAR_TEXT_MAX` for the degraded legs: the converter
    total-escapes its input, so truncated markdown still converts to
    valid HTML, while cutting a converted string could split a tag or an
    entity and 400 the whole leg. The keyboard threads every leg. If the
    plain leg raises, it propagates — the caller's error handling already
    logs and survives plain-send failures.
    """
    if rich:
        try:
            await api.send_rich_message(
                chat_id, reply.markdown, reply_markup=reply.reply_markup
            )
            return
        except BotAPIError:
            pass  # 400 parse / 404 old server / transport → HTML leg
    text = _truncate_inline(reply.markdown, len(reply.markdown), REGULAR_TEXT_MAX)
    try:
        await api.send_message(
            chat_id,
            markdown_to_html(text),
            reply_markup=reply.reply_markup,
            parse_mode="HTML",
        )
        return
    except BotAPIError:
        pass  # e.g. 400 on the HTML payload → plain leg
    await api.send_message(chat_id, text, reply_markup=reply.reply_markup)


async def _send_reply(
    api: BotAPI, chat_id: int, reply: Reply, rich: bool = True
) -> None:
    """Send one dispatch reply to ``chat_id``.

    A ``str`` goes out with ``sendMessage``; a :class:`KeyboardReply`
    with ``sendMessage`` plus the keyboard; a :class:`RichReply` through
    the rich → HTML → plain fallback chain (
    :func:`_send_rich_with_fallback` — the rich leg only when ``rich``);
    a :class:`FileReply` with ``sendPhoto`` (image content types) or
    ``sendDocument``, with its caption and (when set) keyboard. Both
    dispatch paths (message and callback) share this helper, so failed
    sends are caught by the same error handling everywhere.
    """
    if isinstance(reply, RichReply):
        await _send_rich_with_fallback(api, chat_id, reply, rich)
    elif isinstance(reply, FileReply):
        if reply.is_image:
            await api.send_photo(
                chat_id,
                reply.filename,
                reply.data,
                reply.content_type,
                reply.caption,
                reply.reply_markup,
            )
        else:
            await api.send_document(
                chat_id,
                reply.filename,
                reply.data,
                reply.content_type,
                reply.caption,
                reply.reply_markup,
            )
    elif isinstance(reply, KeyboardReply):
        await api.send_message(chat_id, reply.text, reply_markup=reply.reply_markup)
    else:
        await api.send_message(chat_id, reply)


async def run_bot(
    api: BotAPI,
    dispatch: Callable[..., Optional[Union[Reply, Awaitable[Reply]]]],
    stop_event: Optional[asyncio.Event] = None,
    poll_timeout: int = POLL_TIMEOUT,
    error_delay: float = 1.0,
    on_cycle: Optional[Callable[[], Any]] = None,
    callback_dispatch: Optional[Callable[[dict], Optional[CallbackAction]]] = None,
    rich: bool = True,
) -> None:
    """Long-poll ``getUpdates`` and dispatch message handlers until stopped.

    The offset advances to ``update_id + 1`` after each processed update.
    A string reply is sent with ``sendMessage``; a :class:`KeyboardReply`
    with ``sendMessage`` plus its inline keyboard; a :class:`RichReply`
    through the rich → HTML → plain fallback chain (the rich leg only when
    ``rich``); a :class:`FileReply` is sent with ``sendPhoto`` (image
    content types) or ``sendDocument``, so failed file sends are caught by
    the same error handling as failed messages.

    A message carrying a document or a photo (and no text) is extracted
    to an :class:`IncomingFile` and passed to ``dispatch`` as its
    ``file`` argument (the ``/attach`` flow); when the dispatch layer
    answers with a coroutine (its async file handler), ``run_bot`` awaits
    it — every other dispatch layer stays synchronous, so awaiting a
    coroutine is the only async convention the dispatch contract adds.

    ``callback_query`` updates are routed to ``callback_dispatch`` (when
    provided): the raw update dict is passed through, and the callback is
    answered first (``answerCallbackQuery`` — the client's progress bar
    hangs until it is answered), then the action's in-place edit
    (``editMessageText``; skipped when the original message is no longer
    accessible — old messages arrive without a ``message_id``) and/or new
    reply go out through the same send paths as a message reply. A
    :class:`RichMessageEdit` rides the rich ``editMessageText`` payload
    (a text edit of a rich original fails server-side); when a rich edit
    fails, the in-place edit is skipped and the content is re-sent as a
    fresh message through the same send chain (the answer toast has
    already gone out). A missing or ``None`` action answers with
    :data:`UNKNOWN_CALLBACK_TEXT` — the safety net for stale buttons —
    and sends nothing. Callback handling failures are logged and survive
    like message failures.

    Transient :class:`BotAPIError` failures are logged and
    retried after ``error_delay``; they never stop the loop. After each
    successful ``getUpdates`` batch, ``on_cycle`` (the state-change
    notifier) runs; its failures — Bot API or store — are logged to stderr
    and retried on the next cycle, and on a failed poll it is skipped
    entirely. Returns when ``stop_event`` is set; a set ``stop_event``
    interrupts an in-flight ``getUpdates`` — the poll request is cancelled
    and the loop returns immediately instead of waiting for the long poll
    to come back — and a cancelled poll does not advance the offset.
    """
    offset: Optional[int] = None
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        poll = asyncio.ensure_future(
            api.get_updates(offset=offset, timeout=poll_timeout)
        )
        stop_wait = (
            asyncio.ensure_future(stop_event.wait()) if stop_event is not None else None
        )
        try:
            await asyncio.wait(
                {poll, stop_wait} if stop_wait is not None else {poll},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (poll, stop_wait):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        if poll.cancelled():
            return  # stop event fired while the poll was in flight
        try:
            updates = poll.result()
        except BotAPIError as exc:
            print(f"yask: telegram poll failed: {exc}", file=sys.stderr)
            if stop_event is not None and stop_event.is_set():
                return
            await asyncio.sleep(error_delay)
            continue
        for update in updates:
            update_id = update.get("update_id")
            message = update.get("message")
            callback = update.get("callback_query")
            try:
                if message is not None:
                    chat = message.get("chat") or {}
                    text = message.get("text")
                    # A document/photo message (no text) rides the
                    # dispatch layer's file parameter (the /attach flow);
                    # text messages and other media pass file=None.
                    incoming = (
                        _extract_incoming_file(message) if text is None else None
                    )
                    reply = dispatch(text, chat.get("id"), file=incoming)
                    if inspect.iscoroutine(reply):
                        reply = await reply
                    if reply is not None and "id" in chat:
                        await _send_reply(api, chat["id"], reply, rich)
                elif callback is not None:
                    action = (
                        callback_dispatch(callback)
                        if callback_dispatch is not None
                        else None
                    )
                    if action is None:
                        action = CallbackAction(answer_text=UNKNOWN_CALLBACK_TEXT)
                    # Answer first: the client's progress bar hangs until
                    # the callback is answered.
                    await api.answer_callback_query(
                        callback.get("id"),
                        text=action.answer_text,
                        show_alert=action.show_alert,
                        cache_time=action.cache_time,
                    )
                    cb_message = callback.get("message") or {}
                    cb_chat = cb_message.get("chat") or {}
                    if action.edit is not None:
                        # Old messages arrive without a message_id (or
                        # chat): the edit is skipped, the answer is not.
                        message_id = cb_message.get("message_id")
                        if message_id is not None and "id" in cb_chat:
                            if isinstance(action.edit, RichMessageEdit):
                                # A rich original: the edit rides the
                                # rich payload (Bot API 10.1) — a text
                                # edit of a rich message 400s
                                # server-side, so there is no plain edit
                                # leg below. A rich-edit failure (400
                                # parse, 404 on an old local Bot API
                                # server, message gone, transport) skips
                                # the in-place edit and re-sends the
                                # content as a fresh message through the
                                # regular send chain — the answer toast
                                # has already gone out, so the user is
                                # confirmed either way. A failure of the
                                # fresh send propagates to the per-update
                                # catch below (loop survives).
                                try:
                                    await api.edit_message_text(
                                        cb_chat["id"],
                                        message_id,
                                        rich_markdown=action.edit.markdown,
                                        reply_markup=action.edit.reply_markup,
                                    )
                                except BotAPIError as exc:
                                    print(
                                        f"yask: telegram rich edit failed: {exc}",
                                        file=sys.stderr,
                                    )
                                    await _send_reply(
                                        api,
                                        cb_chat["id"],
                                        RichReply(
                                            action.edit.markdown,
                                            action.edit.reply_markup,
                                        ),
                                        rich,
                                    )
                            else:
                                await api.edit_message_text(
                                    cb_chat["id"],
                                    message_id,
                                    action.edit.text,
                                    action.edit.reply_markup,
                                )
                    if action.reply is not None and "id" in cb_chat:
                        await _send_reply(api, cb_chat["id"], action.reply, rich)
            except BotAPIError as exc:
                print(f"yask: telegram dispatch failed: {exc}", file=sys.stderr)
                await asyncio.sleep(error_delay)
            if update_id is not None:
                offset = update_id + 1
        if on_cycle is not None:
            try:
                await on_cycle()
            except Exception as exc:
                print(f"yask: telegram notify failed: {exc}", file=sys.stderr)


def _rich_enabled() -> bool:
    """Rich-message kill switch (``YASK_TELEGRAM_RICH``).

    Rich mode is on unless the variable is exactly ``0`` — client
    compatibility is the driver (Telegram Web refused to render rich
    messages as of June 2026). Read once at startup, never per send, so a
    restart-free environment change does not flip behavior mid-process.
    """
    return os.environ.get("YASK_TELEGRAM_RICH") != "0"


async def _amain(
    token: str,
    data_dir: Path,
    client: Optional[httpx.AsyncClient] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> int:
    api = BotAPI(token, client=client)
    # Fail fast on a bad token before touching disk.
    try:
        me = await api.get_me()
    except BotAPIError as exc:
        await api.aclose()
        print(f"yask: invalid Telegram bot token: {exc}", file=sys.stderr)
        return 1
    print(f"yask: telegram bot @{me.get('username')} (data: {data_dir})")

    # Post the command menu at startup (setMyCommands, #65). Logged, never
    # raised: Telegram may be unreachable here and must not abort polling.
    await register_my_commands(api)

    conn = db.connect(data_dir / "yask.db")
    # Bot-initiated actions (created tasks, state moves) are recorded in
    # the history with source "telegram".
    store = Store(conn, source="telegram")

    # Login sessions live in the store: a restart does not log anyone out.
    auth = Auth(store)

    # The rich-message kill switch, read once at startup (not per send).
    rich = _rich_enabled()

    # Seed the notification cursor to the current history maximum so only
    # changes made while this process runs are pushed (no replay on restart).
    notifier = Notifier(api, store, auth, rich)
    notifier.seed()

    # Without an externally supplied stop event (production), SIGINT is the
    # stop trigger.
    loop = None
    handler_installed = False
    if stop_event is None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, stop_event.set)
            handler_installed = True
        except (NotImplementedError, RuntimeError, ValueError, OSError):
            # No signal-handler support here (non-POSIX, or not in the main
            # thread): main() falls back to catching KeyboardInterrupt.
            pass
    try:
        await run_bot(
            api,
            # The api is passed so the dispatch layer's async file handler
            # (the /attach flow) can download incoming files.
            make_dispatch(store, auth, api=api),
            stop_event=stop_event,
            poll_timeout=POLL_TIMEOUT,
            on_cycle=notifier.check,
            callback_dispatch=make_callback_dispatch(store, auth),
            rich=rich,
        )
    finally:
        if handler_installed:
            loop.remove_signal_handler(signal.SIGINT)
        await api.aclose()
        conn.close()
    return 0


def main(
    token: str,
    data_dir: str | Path,
    client: Optional[httpx.AsyncClient] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> int:
    """Sync entry point for the bot process; returns the exit code.

    Validates the token with getMe before touching disk, opens the store
    from ``data_dir``, then runs the poll loop until SIGINT. Exit 0 on a
    clean stop. ``client`` and ``stop_event`` are test seams (injected
    transport, injected stop signal); production passes neither.
    """
    try:
        return asyncio.run(_amain(token, Path(data_dir), client=client, stop_event=stop_event))
    except KeyboardInterrupt:
        print("yask: stopping telegram bot", file=sys.stderr)
        return 0
