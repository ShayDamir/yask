"""Telegram bot process for yask.

Run via ``yask telegram``: validates the bot token (``TELEGRAM_BOT_TOKEN``)
with ``getMe``, opens the yask store, then long-polls the Bot API with
``getUpdates`` (subscribing to messages and inline-keyboard callbacks) and
answers incoming messages. Today the bot answers
``/start``, ``/help``, ``/projects`` (the project list with per-state task
counts, with one inline button per project), ``/tasks`` (the tasks in the
active states — Todo, Planning, In progress and Review — grouped by
project and state, with one inline button per task),
``/task <project> <number|title>`` (one task's details — state, estimate,
description, prerequisites, attachments and recent history — the task
found by number or by case-insensitive title) and ``/attachment
<project> <task> <id>`` (sends one of the task's attachments to the chat
as a file, via ``sendDocument``/``sendPhoto``); all board reads go through
the store. A chat can ``/subscribe <project>`` to receive task
state-change notifications for that project (``/unsubscribe <project>`` to
stop; subscriptions are per chat, per project, and persist in the same
SQLite database). Inline-keyboard callbacks (``callback_query`` updates)
are answered through a second dispatch layer,
:func:`make_callback_dispatch`: every callback is answered (the client's
progress bar hangs until it is answered) and an unrecognized payload gets
a toast instead of a crash; the ``p:`` payload (the per-project buttons of
``/projects``) opens the project's task list view as a new message and the
``t:`` payload (the per-task buttons of ``/tasks``) opens the task's
detail view as a new message. While the bot runs, the
:class:`Notifier` polls ``state_history`` after each successful
``getUpdates`` batch and pushes a
plain-text message per transition to every subscribed chat; latency is at
most one poll interval. Later features of the Telegram interface (Epic #27)
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
    "Use /help to see what I can do."
)

HELP_TEXT = (
    "Commands:\n"
    "/start — introduction\n"
    "/help — this help\n"
    "/projects — list of projects with per-state task counts\n"
    "/tasks [project] — tasks in Todo, Planning, In progress and Review\n"
    "/task <project> <number|title> — task details (state, description, prereqs, attachments, history)\n"
    "/attachment <project> <task> <id> — send me a task's attachment as a file\n"
    "/subscribe [project] — subscribe to task state-change notifications\n"
    "/unsubscribe [project] — stop notifications for a project\n\n"
    "I read the yask board that this process was started with\n"
    "(yask telegram --data DIR). More commands are on the way."
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

TASK_USAGE_TEXT = (
    "Usage: /task <project> <number|title>\n"
    "Shows one task's details: state, estimate, description, prerequisites,\n"
    "attachments and recent history.\n"
    "Example: /task yask 4 or /task yask fix the bug"
)

ATTACHMENT_USAGE_TEXT = (
    "Usage: /attachment <project> <task> <attachment-id>\n"
    "Sends one of the task's attachments to this chat as a file.\n"
    "List a task's attachments with /task <project> <number|title>."
)

# Telegram caps a message at 4096 chars; the task view stays well under it
# by capping the description and the visible history.
DESCRIPTION_MAX = 2500
HISTORY_MAX = 10

# Static command table. Store-backed commands (today: /projects, /tasks,
# /task, /attachment, /subscribe, /unsubscribe) live in make_dispatch;
# state-change notifications are pushed by the Notifier on every poll
# cycle. Later features extend the dispatch layer without changing the
# poll loop.
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


def format_task_view(task: dict, project: dict, history: list[dict]) -> str:
    """The ``/task`` detail reply for one task.

    A header line in the ``/tasks`` task-line style (``#n title — type``),
    then the sections that have data: state, estimate (``%g``), parent,
    description (truncated at :data:`DESCRIPTION_MAX` with a total-length
    note), prerequisites, attachments (each with its ``/attachment``
    drill-down reference) and the most recent :data:`HISTORY_MAX` history
    rows (with an earlier-count note). Creation rows (``from_state`` is
    NULL) render as ``created``.
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
    return "\n".join(lines)


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


def task_view(store: Store, arg: Optional[str]) -> str:
    """Format the ``/task <project> <number|title>`` reply.

    The argument mixes a project reference and a task reference, either of
    which may contain spaces; :func:`_split_project` resolves the longest
    project prefix, the rest is the task (number or case-insensitive
    title). No argument, or no task reference after the project, gets the
    usage text; an unresolvable project gets the not-found reply pointing
    at ``/projects``.
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
    return format_task_view(task, project, history)


def attachment_view(store: Store, arg: Optional[str]) -> Union[str, FileReply]:
    """Resolve ``/attachment <project> <task> <attachment-id>``.

    Project by longest prefix, task by number or title (a disambiguation
    list when the title is ambiguous — never a file send), then the
    attachment id (the last word) is looked up scoped to that task. Returns
    a :class:`FileReply` to send as a file, or a usage / not-found text.
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
    return FileReply(
        filename=meta["filename"],
        data=data,
        content_type=meta["content_type"],
        caption=f"#{task['number']} {task['title']} — {meta['filename']}",
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


def format_notification(change: dict) -> str:
    """One plain-text notification for a state-history change.

    ``{project_name}: #{number} {title} — {from_state} → {to_state}
    (/task {project_id} {number})``. The trailing ``/task`` reference
    follows the ``/tasks`` drill-down convention and is answered by the
    ``/task`` command.
    """
    return (
        f"{change['project_name']}: #{change['number']} {change['title']} — "
        f"{change['from_state']} → {change['to_state']} "
        f"(/task {change['project_id']} {change['number']})"
    )


def make_dispatch(store: Store) -> Callable[..., Optional[Reply]]:
    """Build the message→reply dispatcher for a bot bound to ``store``.

    Store-backed commands (today: ``/projects``, ``/tasks``, ``/task``,
    ``/attachment``, ``/subscribe``, ``/unsubscribe``) read the board
    through ``store``; the subscription commands additionally need the
    sender's chat id, hence ``dispatch(text, chat_id)``. Everything else
    falls back to the static :func:`reply_for`. A failure reading the store
    yields a short error reply instead of crashing the long-poll loop.
    ``/task`` and ``/attachment`` resolve to a text reply or a
    :class:`FileReply` (the attachment bytes for a file send); a
    :class:`KeyboardReply` is the same text-plus-keyboard shape for
    inline-keyboard views.
    """

    def dispatch(
        text: Optional[str], chat_id: Optional[int] = None
    ) -> Optional[Reply]:
        cmd = _command_token(text)
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
                return task_view(store, _tasks_arg(text))
            except Exception:
                return TASK_ERROR_TEXT
        if cmd == "/attachment":
            try:
                return attachment_view(store, _tasks_arg(text))
            except Exception:
                return ATTACHMENT_ERROR_TEXT
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


def make_callback_dispatch(
    store: Store,
) -> Callable[[dict], Optional[CallbackAction]]:
    """Build the callback_query→action dispatcher for a bot bound to ``store``.

    The inline-keyboard pipeline's dispatch layer, mirroring
    :func:`make_dispatch`: the raw ``callback_query`` update dict is passed
    through (fields ``id``, ``data``, ``message``, ``from``,
    ``chat_instance``) and the factory returns a
    :class:`CallbackAction` (or None for "nothing to do" — answered with
    the out-of-date toast by ``run_bot``).

    There are two payload families today. ``p:<project-id>`` (the
    per-project buttons of the ``/projects`` view) opens that project's
    task list view — :func:`tasks_view` resolved by id, the same view the
    user would get typing ``/tasks <id>`` — as a new message.
    ``t:<project-id>:<number>`` (the per-task buttons of the ``/tasks``
    view) opens that task's detail view — ``get_task`` + ``get_history`` +
    ``format_task_view`` — as a new message: no toast, no in-place edit of
    the list (the detail view's own buttons land in #47). Both are
    strictly shaped payloads (``:``-separated with the right prefix,
    arity and integer fields); an unknown project or task gets an
    informative text reply (the same wording as the ``/tasks`` and ``/task``
    not-found replies); any other shape returns None for the out-of-date
    toast. The remaining payload families (``a:``/``s:``/``u:``, Epic #43)
    extend this factory's body without touching the poll loop.
    """

    def callback_dispatch(callback_query: dict) -> Optional[CallbackAction]:
        data = callback_query.get("data")
        if not isinstance(data, str):
            return None
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
            # The same view typing "/tasks <id>" would send (including its
            # own t: keyboard when the project has active tasks).
            return CallbackAction(reply=tasks_view(store, str(project_id)))
        if len(parts) != 3 or parts[0] != "t":
            return None
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
        try:
            task = store.get_task(project_id, number)
        except NotFound:
            return CallbackAction(
                reply=f"Task #{number} not found in {project['name']}."
            )
        history = store.get_history(project_id, number)
        return CallbackAction(reply=format_task_view(task, project, history))

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
    chat subscribed to its project. The cursor advances only past a change
    that reached all of its subscribers, so a failed send is retried on the
    next cycle (chats that already received it get a duplicate — accepted,
    a lost change would be worse).
    """

    def __init__(self, api: BotAPI, store: Store) -> None:
        self._api = api
        self._store = store
        self._cursor: int = 0

    def seed(self) -> None:
        """Pin the cursor to the current history maximum (bot startup)."""
        self._cursor = self._store.max_state_history_id()

    async def check(self) -> None:
        """Send the pending state changes to every subscribed chat."""
        changes = self._store.new_state_changes(self._cursor)
        for change in changes:
            for chat_id in self._store.subscribed_chats(change["project_id"]):
                await self._api.send_message(chat_id, format_notification(change))
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
    store = Store(conn)

    # Seed the notification cursor to the current history maximum so only
    # changes made while this process runs are pushed (no replay on restart).
    notifier = Notifier(api, store)
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
            make_dispatch(store),
            stop_event=stop_event,
            poll_timeout=POLL_TIMEOUT,
            on_cycle=notifier.check,
            callback_dispatch=make_callback_dispatch(store),
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
