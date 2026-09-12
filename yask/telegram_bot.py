"""Telegram bot process for yask.

Run via ``yask telegram``: validates the bot token (``TELEGRAM_BOT_TOKEN``)
with ``getMe``, opens the yask store, then long-polls the Bot API with
``getUpdates`` (subscribing to messages and inline-keyboard callbacks) and
answers incoming messages. Board access is password-authenticated: the
permitted chats (a chat id plus a password, stored only as a salted hash in
the store's ``telegram_users`` table and managed from the web UI) may
``/login <password>`` once per bot process run (a restart logs every chat
out); until a chat has authenticated, the board commands and every
inline-keyboard callback answer with an auth-required notice and no board
data, and state-change notifications are not delivered to it. The ungated
commands are ``/start``, ``/help``, ``/login`` and ``/whoami`` (the chat's
own id — the identifier the administrator enters in the web UI). Today the
authenticated bot answers
``/projects`` (the project list with per-state task
counts, with one inline button per project), ``/tasks`` (the tasks in the
active states — Todo, Planning, In progress and Review — grouped by
project and state, with one inline button per task),
``/task <project> <number|title>`` (one task's details — state, estimate,
description, prerequisites, attachments and recent history — the task
found by number or by case-insensitive title), ``/attachment
<project> <task> <id>`` (shows one of the task's attachments — small
markdown/plain text (<16 KB) inline as a message, images as a photo,
larger content as a file, via ``sendDocument``/``sendPhoto``) and
``/move <project> <task> <state>`` (moves a task to another workflow
state — a move that would pull prerequisites along is confirmed with
inline buttons first, nothing is applied before the confirmation); all
board reads and writes go through the store. A chat can ``/subscribe
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
markdown/plain text (<16 KB) inline as a message, images as a photo,
larger content as a file — the ``s:``/``u:`` payload (the
subscribe/unsubscribe toggle of the ``/task`` view) toggles the chat's
subscription and flips the button in place, and the ``m:``/``c:``/``x:``
payload (the per-state buttons of the ``/task`` view) moves the task to a
workflow state in place — a move that would pull prerequisites along is
confirmed first (``c:`` confirms the cascade, ``x:`` cancels). While the
bot runs, the
:class:`Notifier` polls ``state_history`` after each successful
``getUpdates`` batch and pushes a message per transition to every
subscribed chat, each carrying one inline button (payload
``t:<project-id>:<number>``) that opens the task's detail view; latency is
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
import json
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union

import httpx

from . import db
from .store import NotFound, Store

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

HELP_TEXT = (
    "Commands:\n"
    "/start — introduction\n"
    "/help — this help\n"
    "/login <password> — authenticate this chat (required for board commands)\n"
    "/whoami — show this chat's id (give it to the yask administrator)\n"
    "/projects — list of projects with per-state task counts\n"
    "/tasks [project] — tasks in Todo, Planning, In progress and Review\n"
    "/task <project> <number|title> — task details (state, description, prereqs, attachments, history)\n"
    "/move <project> <task> <state> — move a task to another state\n"
    "/attachment <project> <task> <id> — show a task's attachment (small markdown inline, images as a photo)\n"
    "/subscribe [project] — subscribe to task state-change notifications\n"
    "/unsubscribe [project] — stop notifications for a project\n\n"
    "I read the yask board that this process was started with\n"
    "(yask telegram --data DIR). More commands are on the way."
)

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
TASK_ERROR_TEXT = "I could not read the board right now. Please try again."
ATTACHMENT_ERROR_TEXT = "I could not read the board right now. Please try again."
SUBSCRIBE_ERROR_TEXT = (
    "I could not change your subscription right now. Please try again."
)
UNSUBSCRIBE_ERROR_TEXT = (
    "I could not change your subscription right now. Please try again."
)
MOVE_ERROR_TEXT = "I could not write to the board right now. Please try again."

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

# Telegram caps a message at 4096 chars; the task view stays well under it
# by capping the description and the visible history.
DESCRIPTION_MAX = 2500
HISTORY_MAX = 10

# Attachments strictly smaller than this are rendered inline instead of
# being sent as a downloadable document (the story's "<16KB" threshold).
INLINE_MARKDOWN_MAX_SIZE = 16 * 1024
# Char budget for an inline attachment message. Telegram caps a message at
# 4096 (measured in UTF-16 code units); 4000 leaves room for the
# truncation note and non-BMP characters.
INLINE_TEXT_MAX = 4000

# Display cap for the state-change notification's button label. Telegram
# documents no limit on inline button text (only ``callback_data`` is
# hard-capped at 64 bytes, which ``t:<project-id>:<number>`` always
# satisfies), so 64 chars is a compact, safe display choice.
NOTIFICATION_BUTTON_TEXT_MAX = 64

# Static command table. Store-backed commands (today: /login, /whoami,
# /projects, /tasks, /task, /attachment, /move, /subscribe,
# /unsubscribe) live in make_dispatch — the first two because they need
# the store and the auth state, the rest because they read (or, for
# /move, write) the board; state-change notifications are pushed by the
# Notifier on every poll cycle. Later features extend the dispatch layer
# without changing the poll loop.
COMMANDS: dict[str, str] = {
    "/start": START_TEXT,
    "/help": HELP_TEXT,
}


class BotAPIError(Exception):
    """Bot API failure: an ``ok:false`` response or a transport failure.

    Carries the API ``error_code`` (when present) and a human-readable
    ``description``.
    """

    def __init__(self, description: str, error_code: Optional[int] = None) -> None:
        super().__init__(description)
        self.error_code = error_code
        self.description = description

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.description


class Auth:
    """The chats that have logged in during this bot process run.

    The allowlist (who *may* authenticate, with which password) lives in
    the store's ``telegram_users`` table — managed from the web UI, stored
    only as salted hashes. This object only tracks which of those chats
    have actually ``/login``-ed since the process started: sessions are
    deliberately per run, so a bot restart logs every chat out (no tokens
    or expiry bookkeeping in the database).
    """

    def __init__(self) -> None:
        self._authenticated: set[int] = set()

    def is_authenticated(self, chat_id: Optional[int]) -> bool:
        """Whether this chat has logged in during this process run."""
        if chat_id is None:
            return False
        return chat_id in self._authenticated

    def authenticate(self, chat_id: Optional[int], password: str, store: Store) -> bool:
        """Check the chat's password against the store's allowlist.

        On success the chat is marked authenticated for this run. A ``None``
        chat (no sender) and an unknown chat or wrong password all return
        ``False`` — the bot cannot tell the cases apart.
        """
        if chat_id is None:
            return False
        if store.verify_telegram_user(chat_id, password):
            self._authenticated.add(chat_id)
            return True
        return False


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


def reply_for(text: Optional[str]) -> Optional[str]:
    """Reply text for an incoming message, or None if there is nothing to say.

    Non-text messages (stickers, photos, ...) get no reply; unknown input
    gets a short "try /help" hint. A ``/command@botname`` suffix is ignored.
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
    :func:`make_callback_dispatch`). An empty board is just ``Projects:``
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
    :func:`make_callback_dispatch`). A reply with no tasks is a plain
    ``str`` — the Bot API rejects an empty inline keyboard, and there are
    no tap targets anyway.
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
    return KeyboardReply("\n".join(lines), {"inline_keyboard": rows})


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
class MessageEdit:
    """An in-place update of the original message (``editMessageText``).

    A callback action edits the message a button lives in (e.g. the
    subscribe toggle) instead of stacking a new message; the keyboard is
    replaced when ``reply_markup`` is set.
    """

    text: str
    reply_markup: Optional[ReplyMarkup] = None


# Every shape a dispatch layer may return: plain text, text with a
# keyboard, or a file (with an optional keyboard).
Reply = Union[str, KeyboardReply, FileReply]


@dataclass(frozen=True)
class CallbackAction:
    """The result of a callback_query dispatch.

    The dispatcher sets at most one of ``reply`` (send a new message) and
    ``edit`` (update the original message in place); ``run_bot`` answers
    the callback first, then edits, then sends. ``answer_text`` is the
    toast shown under the button (None → answer with no text),
    ``show_alert`` upgrades it to an alert dialog, and ``cache_time`` the
    client-side answer cache TTL in seconds.
    """

    answer_text: Optional[str] = None
    show_alert: bool = False
    cache_time: Optional[int] = None
    reply: Optional[Reply] = None
    edit: Optional[MessageEdit] = None


def _toggle_button(project_id: int, subscribed: bool) -> dict:
    """The subscribe/unsubscribe toggle button of a task view's keyboard.

    ``Subscribe`` (payload ``s:<project-id>``) when the chat is not
    subscribed to the project, ``Unsubscribe`` (payload ``u:<project-id>``)
    when it is; both are answered by :func:`make_callback_dispatch`.
    """
    if subscribed:
        return {"text": "Unsubscribe", "callback_data": f"u:{project_id}"}
    return {"text": "Subscribe", "callback_data": f"s:{project_id}"}


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


def format_task_view(
    task: dict,
    project: dict,
    history: list[dict],
    chat_id: Optional[int] = None,
    subscribed: bool = False,
) -> Reply:
    """The ``/task`` detail reply for one task.

    A header line in the ``/tasks`` task-line style (``#n title — type``),
    then the sections that have data: state, estimate (``%g``), parent,
    description (truncated at :data:`DESCRIPTION_MAX` with a total-length
    note), prerequisites, attachments (each with its ``/attachment``
    drill-down reference) and the most recent :data:`HISTORY_MAX` history
    rows (with an earlier-count note). Creation rows (``from_state`` is
    NULL) render as ``created``.

    Without a ``chat_id`` (the pure formatter) the reply is that text as a
    plain :class:`str`, byte-identical to the no-button form. With a
    ``chat_id`` it is a :class:`KeyboardReply` carrying the same text and an
    inline keyboard: one row per attachment (label = filename, payload
    ``a:<project-id>:<number>:<attachment-id>``, answered by
    :func:`make_callback_dispatch`) in id order, then the workflow-state
    rows (:func:`_state_button_rows` — hidden on an archived task), then
    the subscribe/unsubscribe toggle row (:func:`_toggle_button`, driven by
    ``subscribed``). The keyboard always has at least one row (the toggle),
    so the Bot API's empty-inline-keyboard rejection never triggers.
    """
    lines = [f"#{task['number']} {task['title']} — {task['type']}"]
    lines.append(f"State: {task['state']}")
    if task["estimate"] is not None:
        lines.append(f"Estimate: {task['estimate']:g}")
    if task["parent_number"] is not None:
        lines.append(f"Parent: #{task['parent_number']}")
    description = task["description"] or ""
    if description.strip():
        lines.append("Description:")
        if len(description) > DESCRIPTION_MAX:
            description = (
                f"{description[:DESCRIPTION_MAX]}"
                f"… (truncated, {len(task['description'])} chars total)"
            )
        lines.append(description)
    if task["prerequisites"]:
        lines.append("Prerequisites:")
        for p in task["prerequisites"]:
            lines.append(f"  #{p['number']} {p['title']} — {p['state']}")
    if task["attachments"]:
        lines.append("Attachments:")
        for a in task["attachments"]:
            lines.append(
                f"  {a['id']}. {a['filename']} ({_human_size(a['size'])}) — "
                f"/attachment {project['name']} {task['number']} {a['id']}"
            )
    if history:
        lines.append("History:")
        if len(history) > HISTORY_MAX:
            lines.append(f"  … {len(history) - HISTORY_MAX} earlier transitions")
        for h in history[-HISTORY_MAX:]:
            if h["from_state"] is None:
                lines.append(f"  {h['changed_at']} — created ({h['source']})")
            else:
                lines.append(
                    f"  {h['changed_at']} — {h['from_state']} → {h['to_state']} "
                    f"({h['source']})"
                )
    text = "\n".join(lines)
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
    return KeyboardReply(text, {"inline_keyboard": rows})


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

    With a ``chat_id`` the reply is the task view's keyboard form: the
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


def attachment_reply(meta: dict, data: bytes, task: dict) -> Union[str, FileReply]:
    """The reply for one attachment.

    Small text attachments (``text/markdown`` / ``text/plain`` under
    :data:`INLINE_MARKDOWN_MAX_SIZE`) come back as the decoded content as a
    plain ``str`` (sent with sendMessage — inline in the chat), prefixed
    with the same ``#<number> <title> — <filename>`` context line the file
    caption uses, truncated to :data:`INLINE_TEXT_MAX` with a
    ``… (truncated, N chars total)`` note. Everything else is a
    :class:`FileReply` — images as a photo (already displayed inline by
    Telegram), other content as a document.
    """
    if (
        meta["size"] < INLINE_MARKDOWN_MAX_SIZE
        and meta["content_type"] in ("text/markdown", "text/plain")
    ):
        body = data.decode("utf-8", errors="replace")
        text = f"#{task['number']} {task['title']} — {meta['filename']}\n{body}"
        if len(text) > INLINE_TEXT_MAX:
            note = f"\n… (truncated, {len(body)} chars total)"
            budget = max(0, INLINE_TEXT_MAX - len(note))
            text = text[:budget] + note
        return text
    return FileReply(
        filename=meta["filename"],
        data=data,
        content_type=meta["content_type"],
        caption=f"#{task['number']} {task['title']} — {meta['filename']}",
    )


def attachment_view(store: Store, arg: Optional[str]) -> Union[str, FileReply]:
    """Resolve ``/attachment <project> <task> <attachment-id>``.

    Project by longest prefix, task by number or title (a disambiguation
    list when the title is ambiguous — never an attachment send), then the
    attachment id (the last word) is looked up scoped to that task. Returns
    the attachment's reply — :func:`attachment_reply`: an inline text reply
    for small text attachments, a :class:`FileReply` for images (photo) and
    larger content (document) — or a usage / not-found text.
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
    by the ``/task`` command. The message carries one inline button: label
    ``#<number> <title>`` (truncated to
    :data:`NOTIFICATION_BUTTON_TEXT_MAX` chars), payload
    ``t:<project_id>:<number>`` — answered by :func:`make_callback_dispatch`
    (the ``t:`` handler), which opens the task's detail view. No
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
    return KeyboardReply(text, {"inline_keyboard": [[button]]})


def make_dispatch(store: Store, auth: Optional[Auth] = None) -> Callable[..., Optional[Reply]]:
    """Build the message→reply dispatcher for a bot bound to ``store``.

    Store-backed commands (today: ``/login``, ``/whoami``, ``/projects``,
    ``/tasks``, ``/task``, ``/attachment``, ``/move``, ``/subscribe``,
    ``/unsubscribe``) read the board through ``store``; the subscription
    commands additionally need the sender's chat id, hence
    ``dispatch(text, chat_id)``. Everything else falls back to the static
    :func:`reply_for`. A failure reading (or writing) the store yields a
    short error reply instead of crashing the long-poll loop. ``/task``
    resolves to a text reply and ``/attachment`` to an inline text reply
    (small text attachments) or a :class:`FileReply` (the attachment
    bytes for a file send); a :class:`KeyboardReply` is the same
    text-plus-keyboard shape for inline-keyboard views — ``/move``
    answers with one when the move would pull prerequisites along (the
    confirm/cancel keyboard, nothing applied until the ``c:`` button).

    Board access is gated by ``auth`` (an :class:`Auth`; the production
    bot always passes one): the board commands answer unauthenticated
    chats with :data:`AUTH_REQUIRED_TEXT` and no board data. ``/login``
    (success/failure indistinguishable for unknown chats) and ``/whoami``
    (the sender's own chat id) are ungated. Without an ``auth`` the
    commands are open (the legacy, unauthenticated behavior).
    """

    def _authed(chat_id: Optional[int]) -> bool:
        return auth is None or auth.is_authenticated(chat_id)

    def dispatch(
        text: Optional[str], chat_id: Optional[int] = None
    ) -> Optional[Reply]:
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
                    auth.authenticate(chat_id, " ".join(args), store)
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
        if cmd in (
            "/projects",
            "/tasks",
            "/task",
            "/attachment",
            "/move",
            "/subscribe",
            "/unsubscribe",
        ) and not _authed(chat_id):
            return AUTH_REQUIRED_TEXT
        if cmd == "/projects":
            try:
                return project_view(store)
            except Exception:
                return PROJECTS_ERROR_TEXT
        if cmd == "/tasks":
            try:
                return tasks_view(store, _tasks_arg(text))
            except Exception:
                return TASKS_ERROR_TEXT
        if cmd == "/task":
            try:
                return task_view(store, _tasks_arg(text), chat_id)
            except Exception:
                return TASK_ERROR_TEXT
        if cmd == "/attachment":
            try:
                return attachment_view(store, _tasks_arg(text))
            except Exception:
                return ATTACHMENT_ERROR_TEXT
        if cmd == "/move":
            try:
                return move_view(store, _tasks_arg(text))
            except Exception:
                return MOVE_ERROR_TEXT
        if cmd == "/subscribe":
            if chat_id is None:
                return SUBSCRIBE_ERROR_TEXT
            try:
                return subscribe_view(store, chat_id, _tasks_arg(text))
            except Exception:
                return SUBSCRIBE_ERROR_TEXT
        if cmd == "/unsubscribe":
            if chat_id is None:
                return UNSUBSCRIBE_ERROR_TEXT
            try:
                return unsubscribe_view(store, chat_id, _tasks_arg(text))
            except Exception:
                return UNSUBSCRIBE_ERROR_TEXT
        return reply_for(text)

    return dispatch


def _resolve_move_context(
    store: Store, project_id: int, number: int
) -> Union[CallbackAction, tuple[dict, dict]]:
    """Resolve project + task for the ``m:``/``c:``/``x:`` move families.

    The same resolution as the ``t:`` family, including its not-found
    texts; any other store failure answers with :data:`MOVE_ERROR_TEXT`.
    Returns ``(project, task)`` or a ready-to-return
    :class:`CallbackAction`.
    """
    try:
        project = store.get_project(project_id)
    except NotFound:
        return CallbackAction(
            reply=(
                f"Project '{project_id}' not found. "
                "Use /projects to list projects."
            )
        )
    except Exception:
        return CallbackAction(reply=MOVE_ERROR_TEXT)
    try:
        task = store.get_task(project_id, number)
    except NotFound:
        return CallbackAction(
            reply=f"Task #{number} not found in {project['name']}."
        )
    except Exception:
        return CallbackAction(reply=MOVE_ERROR_TEXT)
    return project, task


def _detail_edit(
    store: Store, project: dict, number: int, callback_query: dict
) -> Union[CallbackAction, MessageEdit]:
    """The in-place edit back to the task's fresh detail view.

    Re-renders :func:`format_task_view` after an ``m:``/``c:``/``x:``
    action (the new state text, the keyboard now excluding the new current
    state); the chat id and subscription state come from the callback's
    message (an inaccessible message, with no chat, gets the plain text).
    A store failure answers :data:`MOVE_ERROR_TEXT` instead.
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

    There are seven payload families. ``p:<project-id>`` (the per-project
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
    markdown/plain text (<16 KB) inline as a message, images as a photo,
    other content as a file — the same resolution as ``/attachment``
    (:func:`attachment_reply`). ``s:<project-id>``/``u:<project-id>`` (the
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
    writes nothing. ``c:<project-id>:<number>:<state-index>`` confirms such
    a cascade (applies it with ``confirm=True``, re-renders the detail in
    place); ``x:<project-id>:<number>`` cancels it (re-renders the plain
    detail, no store change). All seven are strictly shaped payloads
    (``:``-separated with the right prefix, arity, integer fields and — for
    the state index — an in-range value); an unknown project, task or
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

    def callback_dispatch(callback_query: dict) -> Optional[CallbackAction]:
        data = callback_query.get("data")
        if not isinstance(data, str):
            return None
        # Board access: the chat the button was pressed in (the original
        # message's chat.id) must have logged in for this run.
        if auth is not None:
            message = callback_query.get("message") or {}
            chat_id = (message.get("chat") or {}).get("id")
            if not auth.is_authenticated(chat_id):
                return CallbackAction(
                    answer_text=AUTH_REQUIRED_TEXT,
                    reply=AUTH_REQUIRED_TEXT,
                )
        parts = data.split(":")
        if len(parts) == 2 and parts[0] == "p":
            if not parts[1].isdigit():
                return None  # malformed → run_bot's out-of-date toast
            project_id = int(parts[1])
            try:
                store.get_project(project_id)
            except NotFound:
                return CallbackAction(
                    reply=(
                        f"Project '{project_id}' not found. "
                        "Use /projects to list projects."
                    )
                )
            except Exception:
                return CallbackAction(reply=TASKS_ERROR_TEXT)
            # The same view typing "/tasks <id>" would send (including its
            # own t: keyboard when the project has active tasks).
            try:
                view = tasks_view(store, str(project_id))
            except Exception:
                return CallbackAction(reply=TASKS_ERROR_TEXT)
            return CallbackAction(reply=view)
        if len(parts) == 3 and parts[0] == "t":
            try:
                project_id = int(parts[1])
                number = int(parts[2])
            except ValueError:
                return None
            try:
                project = store.get_project(project_id)
            except NotFound:
                return CallbackAction(
                    reply=(
                        f"Project '{project_id}' not found. "
                        "Use /projects to list projects."
                    )
                )
            except Exception:
                return CallbackAction(reply=TASK_ERROR_TEXT)
            try:
                task = store.get_task(project_id, number)
            except NotFound:
                return CallbackAction(
                    reply=f"Task #{number} not found in {project['name']}."
                )
            except Exception:
                return CallbackAction(reply=TASK_ERROR_TEXT)
            try:
                history = store.get_history(project_id, number)
            except Exception:
                return CallbackAction(reply=TASK_ERROR_TEXT)
            # The detail reply carries its own keyboard only when the
            # button's chat is known (an inaccessible message arrives with
            # no chat → the plain text, as before): the toggle button
            # reflects that chat's subscription state.
            message = callback_query.get("message") or {}
            chat_id = (message.get("chat") or {}).get("id")
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
        if len(parts) == 4 and parts[0] == "a":
            if not (
                parts[1].isdigit() and parts[2].isdigit() and parts[3].isdigit()
            ):
                return None  # malformed → run_bot's out-of-date toast
            project_id = int(parts[1])
            number = int(parts[2])
            attachment_id = int(parts[3])
            try:
                project = store.get_project(project_id)
            except NotFound:
                return CallbackAction(
                    reply=(
                        f"Project '{project_id}' not found. "
                        "Use /projects to list projects."
                    )
                )
            except Exception:
                return CallbackAction(reply=ATTACHMENT_ERROR_TEXT)
            try:
                task = store.get_task(project_id, number)
            except NotFound:
                return CallbackAction(
                    reply=f"Task #{number} not found in {project['name']}."
                )
            except Exception:
                return CallbackAction(reply=ATTACHMENT_ERROR_TEXT)
            try:
                meta, data = store.get_task_attachment(
                    project_id, number, attachment_id
                )
            except NotFound:
                return CallbackAction(
                    reply=(
                        f"Attachment {attachment_id} not found on task "
                        f"#{number} ({project['name']}). Use /task "
                        f"{project['name']} {number} to list the task's "
                        "attachments."
                    )
                )
            except Exception:
                return CallbackAction(reply=ATTACHMENT_ERROR_TEXT)
            return CallbackAction(reply=attachment_reply(meta, data, task))
        if len(parts) == 2 and parts[0] in ("s", "u") and parts[1].isdigit():
            project_id = int(parts[1])
            # The family's error text, per prefix (a failed press of the
            # subscribe toggle reports a subscribe failure, etc.).
            error_text = (
                SUBSCRIBE_ERROR_TEXT if parts[0] == "s" else UNSUBSCRIBE_ERROR_TEXT
            )
            try:
                project = store.get_project(project_id)
            except NotFound:
                return CallbackAction(
                    reply=(
                        f"Project '{project_id}' not found. "
                        "Use /projects to list projects."
                    )
                )
            except Exception:
                return CallbackAction(reply=error_text)
            message = callback_query.get("message") or {}
            # A subscription is per chat: an inaccessible message (no chat)
            # cannot be toggled → the out-of-date toast.
            chat_id = (message.get("chat") or {}).get("id")
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
            # message (no text) gets the toast only.
            text = message.get("text")
            if text is None:
                return CallbackAction(answer_text=answer)
            rows = (message.get("reply_markup") or {}).get("inline_keyboard") or []
            return CallbackAction(
                answer_text=answer,
                edit=MessageEdit(
                    text,
                    {"inline_keyboard": _flip_toggle(rows, project_id, data, subscribed)},
                ),
            )
        if len(parts) == 4 and parts[0] in ("m", "c") and all(
            parts[i].isdigit() for i in (1, 2, 3)
        ):
            # The /task view's per-state buttons (m:) and the confirm
            # button (c:). The state index is the payload's last field;
            # out of range → malformed → the out-of-date toast.
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
                # to the confirm keyboard, nothing is written.
                confirm = _confirm_move_markup(
                    project_id, task, target, affected
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
            if not isinstance(detail, MessageEdit):
                return detail
            if len(affected) == 1:
                answer = f"Moved #{number} to {target}."
            else:
                answer = f"Moved {len(affected)} tasks to {target}."
            return CallbackAction(answer_text=answer, edit=detail)
        if len(parts) == 3 and parts[0] == "x" and all(
            parts[i].isdigit() for i in (1, 2)
        ):
            # The confirm keyboard's Cancel: re-render the plain detail,
            # no store change.
            project_id, number = int(parts[1]), int(parts[2])
            resolved = _resolve_move_context(store, project_id, number)
            if not isinstance(resolved, tuple):
                return resolved
            project, task = resolved
            detail = _detail_edit(store, project, number, callback_query)
            if not isinstance(detail, MessageEdit):
                return detail
            return CallbackAction(answer_text="Cancelled.", edit=detail)
        # Any other shape (wrong family, arity, or non-numeric fields):
        # nothing to do → run_bot's out-of-date toast.
        return None

    return callback_dispatch


class BotAPI:
    """Minimal Telegram Bot API client: getMe, getUpdates, sendMessage,
    sendDocument, sendPhoto, answerCallbackQuery, editMessageText.

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

    async def _call(self, method: str, **params: Any) -> Any:
        url = f"{self._base_url}/bot{self._token}/{method}"
        try:
            response = await self._post(url, json=params)
        except httpx.HTTPError as exc:
            raise BotAPIError(f"telegram request failed: {exc}") from exc
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
            raise BotAPIError(f"telegram request failed: {exc}") from exc
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
    ) -> dict:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup:
            params["reply_markup"] = reply_markup
        result = await self._call("sendMessage", **params)
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
        text: str,
        reply_markup: Optional[ReplyMarkup] = None,
    ) -> dict:
        """Update a message's text (and inline keyboard) in place."""
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup:
            params["reply_markup"] = reply_markup
        result = await self._call("editMessageText", **params)
        return result if isinstance(result, dict) else {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


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
    ) -> None:
        self._api = api
        self._store = store
        self._auth = auth
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
                await _send_reply(self._api, chat_id, format_notification(change))
            self._cursor = change["id"]


async def _send_reply(api: BotAPI, chat_id: int, reply: Reply) -> None:
    """Send one dispatch reply to ``chat_id``.

    A ``str`` goes out with ``sendMessage``; a :class:`KeyboardReply`
    with ``sendMessage`` plus the keyboard; a :class:`FileReply` with
    ``sendPhoto`` (image content types) or ``sendDocument``, with its
    caption and (when set) keyboard. Both dispatch paths (message and
    callback) share this helper, so failed sends are caught by the same
    error handling everywhere.
    """
    if isinstance(reply, FileReply):
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
    dispatch: Callable[..., Optional[Reply]],
    stop_event: Optional[asyncio.Event] = None,
    poll_timeout: int = POLL_TIMEOUT,
    error_delay: float = 1.0,
    on_cycle: Optional[Callable[[], Any]] = None,
    callback_dispatch: Optional[Callable[[dict], Optional[CallbackAction]]] = None,
) -> None:
    """Long-poll ``getUpdates`` and dispatch message handlers until stopped.

    The offset advances to ``update_id + 1`` after each processed update.
    A string reply is sent with ``sendMessage``; a :class:`KeyboardReply`
    with ``sendMessage`` plus its inline keyboard; a :class:`FileReply` is
    sent with ``sendPhoto`` (image content types) or ``sendDocument``, so
    failed file sends are caught by the same error handling as failed
    messages.

    ``callback_query`` updates are routed to ``callback_dispatch`` (when
    provided): the raw update dict is passed through, and the callback is
    answered first (``answerCallbackQuery`` — the client's progress bar
    hangs until it is answered), then the action's in-place edit
    (``editMessageText``; skipped when the original message is no longer
    accessible — old messages arrive without a ``message_id``) and/or new
    reply go out through the same send paths as a message reply. A missing
    or ``None`` action answers with :data:`UNKNOWN_CALLBACK_TEXT` — the
    safety net for stale buttons — and sends nothing. Callback handling
    failures are logged and survive like message failures.

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
                    reply = dispatch(message.get("text"), chat.get("id"))
                    if reply is not None and "id" in chat:
                        await _send_reply(api, chat["id"], reply)
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
                            await api.edit_message_text(
                                cb_chat["id"],
                                message_id,
                                action.edit.text,
                                action.edit.reply_markup,
                            )
                    if action.reply is not None and "id" in cb_chat:
                        await _send_reply(api, cb_chat["id"], action.reply)
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

    conn = db.connect(data_dir / "yask.db")
    # Bot-initiated actions (created tasks, state moves) are recorded in
    # the history with source "telegram".
    store = Store(conn, source="telegram")

    # Per-run login state: a restart logs every chat out.
    auth = Auth()

    # Seed the notification cursor to the current history maximum so only
    # changes made while this process runs are pushed (no replay on restart).
    notifier = Notifier(api, store, auth)
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
            make_dispatch(store, auth),
            stop_event=stop_event,
            poll_timeout=POLL_TIMEOUT,
            on_cycle=notifier.check,
            callback_dispatch=make_callback_dispatch(store, auth),
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
