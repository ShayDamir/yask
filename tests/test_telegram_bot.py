"""Tests for the Telegram bot process (``yask telegram``).

All Bot API traffic goes through ``httpx.MockTransport`` — real Telegram is
never called.
"""

import asyncio
import json

import httpx
import pytest

from yask import cli
from yask import telegram_bot

BOT_TOKEN = "12345:TEST"


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def message_update(update_id, text, chat_id=7):
    """A getUpdates payload entry carrying a message (text may be None)."""
    message = {"message_id": 1, "chat": {"id": chat_id}}
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def parse_multipart(request):
    """Split a multipart/form-data request into ``(fields, files)``.

    ``fields`` maps field names to their string values; ``files`` maps file
    field names to ``(filename, bytes)``. As deep as the bot's uploads
    need: plain fields plus one file per request, CRLF line endings (httpx's
    multipart format).
    """
    content_type = request.headers["content-type"]
    boundary = next(
        part.strip()[len("boundary="):].strip('"')
        for part in content_type.split(";")
        if part.strip().startswith("boundary=")
    )
    fields = {}
    files = {}
    for part in request.content.split(b"--" + boundary.encode()):
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        if part in (b"", b"--"):
            continue
        head, _, payload = part.partition(b"\r\n\r\n")
        name = None
        filename = None
        for line in head.split(b"\r\n"):
            if not line.lower().startswith(b"content-disposition:"):
                continue
            for token in line.split(b";"):
                token = token.strip()
                if token.startswith(b'name="'):
                    name = token[6:-1].decode()
                elif token.startswith(b'filename="'):
                    filename = token[10:-1].decode()
        if name is None:
            continue
        if filename is not None:
            files[name] = (filename, payload)
        else:
            fields[name] = payload.decode()
    return fields, files


class Script:
    """Canned Bot API responses plus request recording.

    Each ``get_updates`` entry is either a list of updates (returned as an ok
    response) or a ``(status, body)`` pair for an error response. Once the
    entries are exhausted, getUpdates returns empty results and sets ``stop``
    (when configured), so a bot run always terminates. ``fail_once_with``
    raises once on the first request to simulate a transport failure.
    ``sent_files`` records multipart file uploads (sendDocument/sendPhoto):
    one dict per upload with the method, chat id, caption, filename and
    bytes.
    """

    def __init__(self, get_updates, get_me_ok=True, fail_once_with=None):
        self.get_updates = list(get_updates)
        self.get_me_ok = get_me_ok
        self.fail_once_with = fail_once_with
        self.failed_once = False
        self.sent = []
        self.sent_files = []
        self.offsets = []
        self.stop = None

    def handler(self, request):
        if self.fail_once_with is not None and not self.failed_once:
            self.failed_once = True
            raise self.fail_once_with
        method = request.url.path.rsplit("/", 1)[-1]
        if method in ("sendDocument", "sendPhoto"):
            fields, files = parse_multipart(request)
            field = "document" if method == "sendDocument" else "photo"
            filename, data = files[field]
            self.sent_files.append(
                {
                    "method": method,
                    "chat_id": int(fields["chat_id"]),
                    "caption": fields.get("caption"),
                    "filename": filename,
                    "data": data,
                }
            )
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 99}}
            )
        body = json.loads(request.content)
        if method == "getMe":
            if self.get_me_ok:
                return httpx.Response(
                    200, json={"ok": True, "result": {"id": 1, "username": "yask_test_bot"}}
                )
            return httpx.Response(
                401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
            )
        if method == "getUpdates":
            self.offsets.append(body.get("offset"))
            if self.get_updates:
                entry = self.get_updates.pop(0)
                if isinstance(entry, list):
                    return httpx.Response(200, json={"ok": True, "result": entry})
                status, payload = entry
                return httpx.Response(status, json=payload)
            if self.stop is not None:
                self.stop.set()
            return httpx.Response(200, json={"ok": True, "result": []})
        if method == "sendMessage":
            self.sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
        raise AssertionError(f"unexpected Bot API method: {method}")


def run_bot_until_stop(script, dispatch=None, on_cycle=None, error_delay=0.01):
    """Run run_bot against the script until the script drains (sets stop).

    ``dispatch`` defaults to the static ``reply_for`` (wrapped for the
    two-argument dispatch signature); pass a ``make_dispatch(store)``
    dispatcher for store-backed commands. ``on_cycle`` is passed through to
    ``run_bot`` (the state-change notifier hook).
    """
    script.stop = asyncio.Event()
    if dispatch is None:
        def dispatch(text, chat_id=None):
            return telegram_bot.reply_for(text)

    async def go():
        client = make_client(script.handler)
        api = telegram_bot.BotAPI(BOT_TOKEN, client=client)
        try:
            await telegram_bot.run_bot(
                api,
                dispatch,
                stop_event=script.stop,
                poll_timeout=1,
                error_delay=error_delay,
                on_cycle=on_cycle,
            )
        finally:
            await client.aclose()

    asyncio.run(go())
    return script


def test_cli_telegram_missing_token(capsys, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    constructed = []

    def spy(*args, **kwargs):
        constructed.append(1)
        raise AssertionError("BotAPI must not be constructed without a token")

    monkeypatch.setattr(telegram_bot, "BotAPI", spy)
    code = cli.main(["telegram"])
    assert code == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
    assert constructed == []  # no bot, no network, no disk access


def test_cli_telegram_empty_token(capsys, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")
    code = cli.main(["telegram"])
    assert code == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_cli_telegram_data_flag_accepted(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    code = cli.main(["telegram", "--data", str(tmp_path)])
    assert code == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_cli_telegram_wires_token_to_bot(tmp_path, monkeypatch):
    calls = []

    def fake_bot_main(token, data_dir, client=None, stop_event=None):
        calls.append((token, data_dir, client))
        return 0

    monkeypatch.setattr(telegram_bot, "main", fake_bot_main)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:FAKE")
    code = cli.main(["telegram", "--data", str(tmp_path)])
    assert code == 0
    assert len(calls) == 1
    token, data_dir, client = calls[0]
    assert token == "111:FAKE"
    assert data_dir == tmp_path
    assert client is None  # the CLI never injects a transport


def test_main_invalid_token(tmp_path, capsys):
    script = Script([], get_me_ok=False)
    client = make_client(script.handler)
    code = telegram_bot.main("bad-token", tmp_path, client=client)
    assert code == 1
    assert "invalid Telegram bot token" in capsys.readouterr().err
    # getMe failed before touching disk
    assert not (tmp_path / "yask.db").exists()


def test_main_happy_path_opens_store_and_exits_zero(tmp_path):
    script = Script([[message_update(71, "/start")]])
    script.stop = asyncio.Event()
    client = make_client(script.handler)
    code = telegram_bot.main(BOT_TOKEN, tmp_path, client=client, stop_event=script.stop)
    assert code == 0
    assert (tmp_path / "yask.db").exists()
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == telegram_bot.START_TEXT


def test_start_command():
    script = run_bot_until_stop(Script([[message_update(101, "/start")]]))
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == telegram_bot.START_TEXT
    # offset advanced past the processed update
    assert script.offsets == [None, 102]


def test_help_command():
    script = run_bot_until_stop(Script([[message_update(21, "/help")]]))
    assert len(script.sent) == 1
    text = script.sent[0]["text"]
    assert text == telegram_bot.HELP_TEXT
    assert "/start" in text
    assert "/help" in text
    assert script.offsets == [None, 22]


def test_unknown_text_gets_help_hint():
    script = run_bot_until_stop(Script([[message_update(31, "what is this?")]]))
    assert len(script.sent) == 1
    assert "/help" in script.sent[0]["text"]
    assert script.offsets == [None, 32]


def test_unknown_command_gets_help_hint():
    script = run_bot_until_stop(Script([[message_update(33, "/nope")]]))
    assert len(script.sent) == 1
    assert "/help" in script.sent[0]["text"]


def test_non_text_message_is_ignored():
    script = run_bot_until_stop(Script([[message_update(41, None)]]))
    assert script.sent == []
    assert script.offsets == [None, 42]


def test_command_with_bot_mention():
    script = run_bot_until_stop(Script([[message_update(44, "/start@yask_test_bot")]]))
    assert script.sent and script.sent[0]["text"] == telegram_bot.START_TEXT


def test_run_bot_preset_stop_returns_without_polling():
    script = Script([[message_update(1, "/start")]])
    client = make_client(script.handler)
    api = telegram_bot.BotAPI(BOT_TOKEN, client=client)
    stop = asyncio.Event()
    stop.set()

    def dispatch(text, chat_id=None):
        return telegram_bot.reply_for(text)

    asyncio.run(telegram_bot.run_bot(api, dispatch, stop_event=stop))
    assert script.offsets == []  # no getUpdates after the flag was set
    assert script.sent == []


def test_poll_429_backs_off_and_recovers():
    payload = (
        429,
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 0",
            "parameters": {"retry_after": 0},
        },
    )
    script = run_bot_until_stop(Script([payload, [message_update(51, "/start")]]))
    assert len(script.sent) == 1
    assert script.sent[0]["text"] == telegram_bot.START_TEXT
    assert script.offsets[-1] == 52


def test_transport_error_is_survived():
    script = run_bot_until_stop(
        Script([[message_update(61, "/start")]], fail_once_with=httpx.ConnectError("connection refused"))
    )
    assert len(script.sent) == 1
    assert script.sent[0]["text"] == telegram_bot.START_TEXT
    assert script.offsets[-1] == 62


def test_reply_for_dispatch_table():
    assert telegram_bot.reply_for("/start") == telegram_bot.START_TEXT
    assert telegram_bot.reply_for("/help") == telegram_bot.HELP_TEXT
    assert telegram_bot.reply_for("/start please") == telegram_bot.START_TEXT
    assert "/help" in telegram_bot.reply_for("random chatter")
    assert telegram_bot.reply_for(None) is None
    assert telegram_bot.reply_for("   ") is None


# --- /projects (store-backed dispatch) -------------------------------------


def test_projects_empty_board(store):
    script = run_bot_until_stop(
        Script([[message_update(71, "/projects")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == "Projects:\n(none)"


def test_projects_populated_board(store):
    # ids follow creation order; display order is by name
    yask = store.create_project("yask")["id"]
    side = store.create_project("side-project")["id"]
    zeta = store.create_project("zeta")["id"]

    for i in range(3):
        store.create_task(yask, f"backlog {i}")
    t1 = store.create_task(yask, "todo 1")
    store.move_task(yask, t1["number"], "Todo", confirm=True)
    t2 = store.create_task(yask, "todo 2")
    store.move_task(yask, t2["number"], "Todo", confirm=True)
    t3 = store.create_task(yask, "working")
    store.move_task(yask, t3["number"], "In progress", confirm=True)
    gone = store.create_task(yask, "archived")
    store.archive_task(yask, gone["number"], confirm=True)

    z1 = store.create_task(zeta, "z1")
    store.move_task(zeta, z1["number"], "Blocked")
    z2 = store.create_task(zeta, "z2")
    store.move_task(zeta, z2["number"], "Blocked")

    script = run_bot_until_stop(
        Script([[message_update(81, "/projects")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    # name order, id prefixes, zero states skipped, archived excluded
    assert script.sent[0]["text"] == (
        "Projects:\n"
        f"{side}. side-project\n"
        f"{yask}. yask — Backlog: 3, Todo: 2, In progress: 1\n"
        f"{zeta}. zeta — Blocked: 2"
    )


def test_projects_with_bot_mention(store):
    script = run_bot_until_stop(
        Script([[message_update(84, "/projects@yask_test_bot")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["text"] == "Projects:\n(none)"


def test_projects_unknown_command_still_gets_hint(store):
    script = run_bot_until_stop(
        Script([[message_update(86, "/nope")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert "/help" in script.sent[0]["text"]


def test_help_mentions_projects():
    assert "/projects" in telegram_bot.HELP_TEXT


def test_projects_store_failure_replies_and_recovers(store, monkeypatch):
    def boom():
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "list_project_overviews", boom)
    script = run_bot_until_stop(
        Script(
            [[message_update(91, "/projects"), message_update(92, "/start")]]
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # the failure produces a reply, not a crash; the next message is still
    # answered
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == telegram_bot.PROJECTS_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT


# --- /tasks (store-backed dispatch) ----------------------------------------


def test_tasks_empty_board(store):
    script = run_bot_until_stop(
        Script([[message_update(141, "/tasks")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == "Tasks in progress:\n(none)"


def test_tasks_populated_board_exact(store):
    # zeta is created first (id 1) but "alpha" sorts before "zeta", so the
    # id-2 project is listed first — display order is by name.
    zeta = store.create_project("zeta")["id"]
    alpha = store.create_project("alpha")["id"]

    # alpha: one task in each of the eight states
    store.create_task(alpha, "backlog")                          # #1 Backlog
    t = store.create_task(alpha, "todo 1")
    store.move_task(alpha, t["number"], "Todo", confirm=True)         # #2
    t = store.create_task(alpha, "planning 1")
    store.move_task(alpha, t["number"], "Planning", confirm=True)     # #3
    t = store.create_task(alpha, "working")
    store.move_task(alpha, t["number"], "In progress", confirm=True)  # #4
    t = store.create_task(alpha, "review 1")
    store.move_task(alpha, t["number"], "Review", confirm=True)       # #5
    t = store.create_task(alpha, "done")
    store.move_task(alpha, t["number"], "Done", confirm=True)         # #6
    t = store.create_task(alpha, "blocked")
    store.move_task(alpha, t["number"], "Blocked")                    # #7
    t = store.create_task(alpha, "archived")
    store.archive_task(alpha, t["number"], confirm=True)              # #8

    # zeta: a single in-progress task
    t = store.create_task(zeta, "z working")
    store.move_task(zeta, t["number"], "In progress", confirm=True)

    script = run_bot_until_stop(
        Script([[message_update(101, "/tasks")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    # exact text: name order, state grouping/order, id prefixes, per-task
    # drill-down links, and the excluded states absent
    assert script.sent[0]["text"] == (
        "Tasks in progress:\n"
        f"{alpha}. alpha\n"
        "  Todo:\n"
        f"    #2 todo 1 — /task {alpha} 2\n"
        "  Planning:\n"
        f"    #3 planning 1 — /task {alpha} 3\n"
        "  In progress:\n"
        f"    #4 working — /task {alpha} 4\n"
        "  Review:\n"
        f"    #5 review 1 — /task {alpha} 5\n"
        f"{zeta}. zeta\n"
        "  In progress:\n"
        f"    #1 z working — /task {zeta} 1"
    )


def test_tasks_filter_by_id_and_name(store):
    zeta = store.create_project("zeta")["id"]
    alpha = store.create_project("alpha")["id"]
    t = store.create_task(alpha, "alpha working")
    store.move_task(alpha, t["number"], "In progress", confirm=True)
    t = store.create_task(zeta, "zeta working")
    store.move_task(zeta, t["number"], "In progress", confirm=True)

    # by project name, case-insensitive
    script = run_bot_until_stop(
        Script([[message_update(111, "/tasks ALPHA")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Tasks in progress:\n"
        f"{alpha}. alpha\n"
        "  In progress:\n"
        f"    #1 alpha working — /task {alpha} 1"
    )

    # by project id
    script = run_bot_until_stop(
        Script([[message_update(112, f"/tasks {zeta}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Tasks in progress:\n"
        f"{zeta}. zeta\n"
        "  In progress:\n"
        f"    #1 zeta working — /task {zeta} 1"
    )


def test_tasks_filter_name_with_spaces(store):
    pid = store.create_project("my big project")["id"]
    t = store.create_task(pid, "working")
    store.move_task(pid, t["number"], "In progress", confirm=True)
    script = run_bot_until_stop(
        Script([[message_update(171, "/tasks my big project")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Tasks in progress:\n"
        f"{pid}. my big project\n"
        "  In progress:\n"
        f"    #1 working — /task {pid} 1"
    )


def test_tasks_filter_zero_active(store):
    alpha = store.create_project("alpha")["id"]
    zeta = store.create_project("zeta")["id"]
    # alpha has only Backlog/Done tasks (no active state)
    store.create_task(alpha, "backlog only")
    t = store.create_task(alpha, "done only")
    store.move_task(alpha, t["number"], "Done", confirm=True)
    # zeta has an active task, so the board is non-empty overall
    t = store.create_task(zeta, "z working")
    store.move_task(zeta, t["number"], "In progress", confirm=True)

    # project found but zero active tasks
    script = run_bot_until_stop(
        Script([[message_update(121, "/tasks alpha")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == "Tasks in progress:\n(none)"


def test_tasks_unknown_project(store):
    store.create_project("alpha")
    script = run_bot_until_stop(
        Script([[message_update(131, "/tasks nope")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project 'nope' not found. Use /projects to list projects."
    )
    # unknown by id
    script = run_bot_until_stop(
        Script([[message_update(132, "/tasks 999")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project '999' not found. Use /projects to list projects."
    )


def test_tasks_with_bot_mention(store):
    script = run_bot_until_stop(
        Script([[message_update(151, "/tasks@yask_test_bot")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == "Tasks in progress:\n(none)"


def test_tasks_unknown_command_still_gets_hint(store):
    script = run_bot_until_stop(
        Script([[message_update(155, "/nope")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] and "/help" in script.sent[0]["text"]


def test_tasks_store_failure_replies_and_recovers(store, monkeypatch):
    # a project must exist so the no-arg view reaches list_in_progress
    store.create_project("alpha")

    def boom(project_id):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "list_in_progress", boom)
    script = run_bot_until_stop(
        Script([[message_update(161, "/tasks"), message_update(162, "/start")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # the failure produces a reply, not a crash; the next message is still
    # answered
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == telegram_bot.TASKS_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT


def test_help_mentions_tasks():
    assert "/tasks" in telegram_bot.HELP_TEXT


# --- /task (store-backed dispatch) -------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100  # 108 bytes


def seed_task_view(store):
    """Seed a yask project with one fully-equipped task (the /task tests).

    Epic #1 contains task "working" (a Story with estimate, description and
    an attachment pair); "prereq one" sits in Review and "prereq two" ends up
    pulled to In progress by "working"'s moves. Returns ids, attachment
    metadata and the exact attachment bytes.
    """
    pid = store.create_project("yask")["id"]
    epic = store.create_task(pid, "epic", "Epic")
    p1 = store.create_task(pid, "prereq one")
    store.move_task(pid, p1["number"], "Review", confirm=True)
    p2 = store.create_task(pid, "prereq two")
    t = store.create_task(
        pid,
        "working",
        "Story",
        estimate=3.0,
        description="The task at hand.",
        parent_number=epic["number"],
    )
    store.set_prerequisites(pid, t["number"], [p1["number"], p2["number"]])
    store.move_task(pid, t["number"], "Todo", confirm=True)
    store.move_task(pid, t["number"], "In progress", confirm=True)
    plan_bytes = b"# Plan\n" + b"x" * 7793  # 7800 bytes → "7.6 KB"
    plan = store.add_attachment(
        pid, t["number"], "plan.md", "text/markdown", plan_bytes
    )
    img = store.add_attachment(pid, t["number"], "img.png", "image/png", PNG)
    return {
        "pid": pid,
        "epic": epic,
        "p1": p1,
        "p2": p2,
        "t": t,
        "plan": plan,
        "img": img,
        "plan_bytes": plan_bytes,
    }


def expected_task_text(store, d):
    """The exact /task detail reply for the seed_task_view task."""
    pid, t = d["pid"], d["t"]
    history = store.get_history(pid, t["number"])
    assert len(history) == 3
    return (
        f"#{t['number']} working — Story\n"
        "State: In progress\n"
        "Estimate: 3\n"
        f"Parent: #{d['epic']['number']}\n"
        "Description:\n"
        "The task at hand.\n"
        "Prerequisites:\n"
        f"  #{d['p1']['number']} prereq one — Review\n"
        f"  #{d['p2']['number']} prereq two — In progress\n"
        "Attachments:\n"
        f"  {d['plan']['id']}. plan.md (7.6 KB) — /attachment yask "
        f"{t['number']} {d['plan']['id']}\n"
        f"  {d['img']['id']}. img.png (108 B) — /attachment yask "
        f"{t['number']} {d['img']['id']}\n"
        "History:\n"
        f"  {history[0]['changed_at']} — created (web)\n"
        f"  {history[1]['changed_at']} — Backlog → Todo (web)\n"
        f"  {history[2]['changed_at']} — Todo → In progress (web)"
    )


def test_task_by_number_exact(store):
    d = seed_task_view(store)
    script = run_bot_until_stop(
        Script([[message_update(231, f"/task yask {d['t']['number']}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == expected_task_text(store, d)


def test_task_by_title_case_insensitive(store):
    d = seed_task_view(store)
    script = run_bot_until_stop(
        Script([[message_update(232, "/task yask WORKING")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["text"] == expected_task_text(store, d)


def test_task_title_two_matches_disambiguates(store):
    pid = store.create_project("yask")["id"]
    t1 = store.create_task(pid, "fix bug")
    store.move_task(pid, t1["number"], "Todo", confirm=True)
    t2 = store.create_task(pid, "fix bug")
    script = run_bot_until_stop(
        Script([[message_update(233, "/task yask fix bug")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Several tasks in yask match 'fix bug':\n"
        f"  #{t1['number']} fix bug — Todo\n"
        f"  #{t2['number']} fix bug — Backlog\n"
        "Use /task yask <number>."
    )


def test_task_unknown_number_and_title(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "working")
    script = run_bot_until_stop(
        Script([[message_update(234, "/task yask 99")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == "Task #99 not found in yask."
    script = run_bot_until_stop(
        Script([[message_update(235, "/task yask nope")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == "Task 'nope' not found in yask."


def test_task_unknown_project(store):
    store.create_project("yask")
    script = run_bot_until_stop(
        Script([[message_update(236, "/task nope 1")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project 'nope' not found. Use /projects to list projects."
    )
    script = run_bot_until_stop(
        Script([[message_update(237, "/task 999 1")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project '999' not found. Use /projects to list projects."
    )


def test_task_numeric_title_fallback(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "alpha")
    t = store.create_task(pid, "2024")
    script = run_bot_until_stop(
        Script([[message_update(238, "/task yask 2024")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # no task #2024 — the all-digit reference falls back to the title
    assert script.sent[0]["text"].startswith(f"#{t['number']} 2024 — Task\n")


def test_task_number_wins_over_same_title(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "2024")
    two = store.create_task(pid, "real two")
    script = run_bot_until_stop(
        Script([[message_update(239, "/task yask 2")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"].startswith(
        f"#{two['number']} real two — Task\n"
    )


def test_task_project_and_title_with_spaces(store):
    pid = store.create_project("my big project")["id"]
    store.create_task(pid, "other")
    t = store.create_task(pid, "fix the bug")
    script = run_bot_until_stop(
        Script([[message_update(240, "/task my big project fix the bug")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # longest-prefix project resolution: "my big project" + title "fix the bug"
    assert script.sent[0]["text"].startswith(f"#{t['number']} fix the bug — Task\n")


def test_task_archived_by_number_not_by_title(store):
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "doomed")
    store.archive_task(pid, t["number"], confirm=True)
    script = run_bot_until_stop(
        Script([[message_update(241, f"/task yask {t['number']}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"].startswith(f"#{t['number']} doomed — Task\n")
    assert "State: Archived" in script.sent[0]["text"]
    # the title search excludes archived tasks
    script = run_bot_until_stop(
        Script([[message_update(242, "/task yask doomed")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == "Task 'doomed' not found in yask."


def test_task_with_bot_mention(store):
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    script = run_bot_until_stop(
        Script([[message_update(243, f"/task@yask_test_bot yask {t['number']}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"].startswith(f"#{t['number']} working — Task\n")


def test_task_usage_texts(store):
    store.create_project("yask")
    script = run_bot_until_stop(
        Script([[message_update(244, "/task")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == telegram_bot.TASK_USAGE_TEXT
    # a project with no task reference after it gets the usage text too
    script = run_bot_until_stop(
        Script([[message_update(245, "/task yask")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == telegram_bot.TASK_USAGE_TEXT


def test_task_long_description_truncated(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "chatty", description="x" * 4000)
    script = run_bot_until_stop(
        Script([[message_update(246, "/task yask chatty")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    text = script.sent[0]["text"]
    assert "… (truncated, 4000 chars total)" in text
    # capped at exactly 2500 description chars
    assert "x" * 2500 in text
    assert "x" * 2501 not in text
    # the whole reply stays under Telegram's message cap
    assert len(text) <= 4096


def test_task_history_capped_at_ten(store):
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "moving")
    for s in [
        "Todo", "Planning", "In progress", "Review", "Done",
        "Backlog", "Todo", "Planning", "In progress", "Review", "Done",
    ]:
        store.move_task(pid, t["number"], s)
    history = store.get_history(pid, t["number"])
    assert len(history) == 12
    script = run_bot_until_stop(
        Script([[message_update(247, f"/task yask {t['number']}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    lines = script.sent[0]["text"].split("\n")
    idx = lines.index("History:")
    assert lines[idx:] == (
        ["History:", f"  … {12 - telegram_bot.HISTORY_MAX} earlier transitions"]
        + [
            f"  {h['changed_at']} — {h['from_state']} → {h['to_state']} "
            f"({h['source']})"
            for h in history[-telegram_bot.HISTORY_MAX:]
        ]
    )


def test_tasks_drill_down_to_task_view(store):
    """The /task reference emitted by /tasks must resolve to the detail view."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(
        pid, "drill down", "Story", estimate=2.0, description="the point"
    )
    store.move_task(pid, t["number"], "In progress", confirm=True)
    # the reference exactly as /tasks emits it (the cross-task contract)
    first_reply = telegram_bot.tasks_view(store, str(pid))
    ref = first_reply.splitlines()[-1].rsplit(" — ", 1)[-1]
    assert ref == f"/task {pid} {t['number']}"
    script = run_bot_until_stop(
        Script(
            [
                [message_update(248, "/tasks")],
                [message_update(249, ref)],
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 2
    task_line = f"    #{t['number']} drill down — /task {pid} {t['number']}"
    assert script.sent[0]["text"].splitlines()[-1] == task_line
    # re-sending the reference yields the detail view
    assert script.sent[1]["text"].startswith(f"#{t['number']} drill down — Story\n")
    assert "State: In progress" in script.sent[1]["text"]
    assert "Estimate: 2" in script.sent[1]["text"]


def test_task_store_failure_replies_and_recovers(store, monkeypatch):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "working")

    def boom(project_id, number):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "get_task", boom)
    script = run_bot_until_stop(
        Script([[message_update(250, "/task yask 1"), message_update(251, "/start")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # the failure produces a reply, not a crash; the next message is still
    # answered
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == telegram_bot.TASK_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT


def test_help_mentions_task_and_attachment():
    assert "/task" in telegram_bot.HELP_TEXT
    assert "/attachment" in telegram_bot.HELP_TEXT


# --- /attachment (store-backed dispatch) -------------------------------------


def test_attachment_markdown_sent_as_document(store):
    d = seed_task_view(store)
    script = run_bot_until_stop(
        Script(
            [
                [
                    message_update(
                        261, f"/attachment yask {d['t']['number']} {d['plan']['id']}"
                    )
                ]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent == []  # a file, not a text message
    assert len(script.sent_files) == 1
    f = script.sent_files[0]
    assert f["method"] == "sendDocument"
    assert f["chat_id"] == 7
    assert f["filename"] == "plan.md"
    assert f["data"] == d["plan_bytes"]
    assert f["caption"] == f"#{d['t']['number']} working — plan.md"


def test_attachment_image_sent_as_photo(store):
    d = seed_task_view(store)
    script = run_bot_until_stop(
        Script(
            [
                [
                    message_update(
                        262, f"/attachment yask {d['t']['number']} {d['img']['id']}"
                    )
                ]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent == []
    assert len(script.sent_files) == 1
    f = script.sent_files[0]
    assert f["method"] == "sendPhoto"
    assert f["chat_id"] == 7
    assert f["filename"] == "img.png"
    assert f["data"] == PNG
    assert f["caption"] == f"#{d['t']['number']} working — img.png"


def test_attachment_by_task_title(store):
    d = seed_task_view(store)
    script = run_bot_until_stop(
        Script(
            [
                [
                    message_update(
                        263, f"/attachment yask working {d['plan']['id']}"
                    )
                ]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent == []
    assert len(script.sent_files) == 1
    assert script.sent_files[0]["filename"] == "plan.md"
    assert script.sent_files[0]["data"] == d["plan_bytes"]


def test_attachment_usage_and_not_found_texts(store):
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    meta = store.add_attachment(
        pid, t["number"], "a.md", "text/markdown", b"a"
    )
    # no argument
    script = run_bot_until_stop(
        Script([[message_update(264, "/attachment")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == telegram_bot.ATTACHMENT_USAGE_TEXT
    # a project with no task reference after it
    script = run_bot_until_stop(
        Script([[message_update(265, "/attachment yask")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == telegram_bot.ATTACHMENT_USAGE_TEXT
    # unknown task
    script = run_bot_until_stop(
        Script([[message_update(266, f"/attachment yask 99 {meta['id']}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == "Task #99 not found in yask."
    # unknown project (name and id)
    script = run_bot_until_stop(
        Script([[message_update(267, "/attachment nope 1 1")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project 'nope' not found. Use /projects to list projects."
    )
    script = run_bot_until_stop(
        Script([[message_update(268, "/attachment 999 1 1")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project '999' not found. Use /projects to list projects."
    )
    assert script.sent_files == []


def test_attachment_foreign_id_not_found(store):
    pid = store.create_project("yask")["id"]
    a = store.create_task(pid, "a")
    b = store.create_task(pid, "b")
    meta = store.add_attachment(pid, a["number"], "a.md", "text/markdown", b"a")
    script = run_bot_until_stop(
        Script(
            [[message_update(269, f"/attachment yask {b['number']} {meta['id']}")]],
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent_files == []
    assert script.sent[0]["text"] == (
        f"Attachment {meta['id']} not found on task #{b['number']} (yask). "
        f"Use /task yask {b['number']} to list the task's attachments."
    )


def test_attachment_store_failure_replies_and_recovers(store, monkeypatch):
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    store.add_attachment(pid, t["number"], "a.md", "text/markdown", b"a")

    def boom(project_id, number, attachment_id):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "get_task_attachment", boom)
    script = run_bot_until_stop(
        Script(
            [
                [
                    message_update(270, f"/attachment yask {t['number']} 1"),
                    message_update(271, "/start"),
                ]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == telegram_bot.ATTACHMENT_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT
    assert script.sent_files == []


# --- /subscribe, /unsubscribe (store-backed dispatch) ------------------------


def test_subscribe_confirms_and_stores(store):
    pid = store.create_project("yask")["id"]
    script = run_bot_until_stop(
        Script([[message_update(201, "/subscribe yask")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == (
        f"Subscribed to yask ({pid}) — you will be notified about "
        "task state changes in this project."
    )
    assert [s["project_id"] for s in store.list_subscriptions(7)] == [pid]


def test_subscribe_by_project_id(store):
    pid = store.create_project("yask")["id"]
    script = run_bot_until_stop(
        Script([[message_update(202, f"/subscribe {pid}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"].startswith(f"Subscribed to yask ({pid})")
    assert [s["project_id"] for s in store.list_subscriptions(7)] == [pid]


def test_subscribe_no_arg_lists_subscriptions(store):
    script = run_bot_until_stop(
        Script([[message_update(203, "/subscribe")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == "Your subscriptions:\n(none)"

    zeta = store.create_project("zeta")["id"]
    alpha = store.create_project("alpha")["id"]
    store.subscribe_project(7, zeta)
    store.subscribe_project(7, alpha)
    script = run_bot_until_stop(
        Script([[message_update(204, "/subscribe")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # same shape as /projects: id-prefixed lines in name order
    assert script.sent[0]["text"] == (
        "Your subscriptions:\n"
        f"{alpha}. alpha\n"
        f"{zeta}. zeta"
    )


def test_subscribe_unknown_project(store):
    store.create_project("alpha")
    script = run_bot_until_stop(
        Script([[message_update(205, "/subscribe nope")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project 'nope' not found. Use /projects to list projects."
    )
    assert store.list_subscriptions(7) == []
    script = run_bot_until_stop(
        Script([[message_update(206, "/subscribe 999")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project '999' not found. Use /projects to list projects."
    )
    assert store.list_subscriptions(7) == []


def test_unsubscribe_flow(store):
    pid = store.create_project("yask")["id"]
    store.subscribe_project(7, pid)

    script = run_bot_until_stop(
        Script([[message_update(211, "/unsubscribe yask")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == f"Unsubscribed from yask ({pid})."
    assert store.list_subscriptions(7) == []

    # nothing left to remove
    script = run_bot_until_stop(
        Script([[message_update(212, "/unsubscribe yask")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == f"You are not subscribed to yask ({pid})."

    # unknown project
    script = run_bot_until_stop(
        Script([[message_update(213, "/unsubscribe nope")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project 'nope' not found. Use /projects to list projects."
    )


def test_subscribe_with_bot_mention(store):
    pid = store.create_project("yask")["id"]
    script = run_bot_until_stop(
        Script([[message_update(215, "/subscribe@yask_test_bot yask")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"].startswith(f"Subscribed to yask ({pid})")
    assert [s["project_id"] for s in store.list_subscriptions(7)] == [pid]


def test_subscribe_store_failure_replies_and_recovers(store, monkeypatch):
    store.create_project("yask")

    def boom(chat_id, project_id):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "subscribe_project", boom)
    script = run_bot_until_stop(
        Script([[message_update(217, "/subscribe yask"), message_update(218, "/start")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # the failure produces a reply, not a crash; the next message is still
    # answered
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == telegram_bot.SUBSCRIBE_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT


def test_unsubscribe_store_failure_replies_and_recovers(store, monkeypatch):
    store.create_project("yask")

    def boom(chat_id, project_id):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "unsubscribe_project", boom)
    script = run_bot_until_stop(
        Script([[message_update(219, "/unsubscribe yask"), message_update(220, "/start")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == telegram_bot.UNSUBSCRIBE_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT


def test_help_mentions_subscribe():
    assert "/subscribe" in telegram_bot.HELP_TEXT
    assert "/unsubscribe" in telegram_bot.HELP_TEXT


# --- state-change notifications (the Notifier) --------------------------------


class FakeAPI:
    """Records send_message calls; fails for the ids in ``fail_for``."""

    def __init__(self, fail_for=None):
        self.sent = []
        self.fail_for = set(fail_for or ())

    async def send_message(self, chat_id, text):
        if chat_id in self.fail_for:
            raise telegram_bot.BotAPIError("simulated send failure")
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}


def _moved(store, pid, title="working", to_state="Review"):
    t = store.create_task(pid, title)
    store.move_task(pid, t["number"], to_state, confirm=True)
    return t


def _notification(pid, t, title, to_state):
    return (
        f"yask: #{t['number']} {title} — Backlog → {to_state} "
        f"(/task {pid} {t['number']})"
    )


def test_notifier_seed_suppresses_pre_seed_history(store):
    pid = store.create_project("yask")["id"]
    _moved(store, pid, "old")  # happens before the bot starts
    api = FakeAPI()
    notifier = telegram_bot.Notifier(api, store)
    notifier.seed()
    asyncio.run(notifier.check())
    assert api.sent == []


def test_notifier_sends_each_change_once(store):
    pid = store.create_project("yask")["id"]
    store.subscribe_project(7, pid)
    api = FakeAPI()
    notifier = telegram_bot.Notifier(api, store)
    notifier.seed()
    t = _moved(store, pid)
    asyncio.run(notifier.check())
    assert api.sent == [(7, _notification(pid, t, "working", "Review"))]
    # a second cycle sends nothing new
    asyncio.run(notifier.check())
    assert api.sent == [(7, _notification(pid, t, "working", "Review"))]


def test_notifier_without_subscribers_sends_nothing(store):
    pid = store.create_project("yask")["id"]
    api = FakeAPI()
    notifier = telegram_bot.Notifier(api, store)
    notifier.seed()
    _moved(store, pid)
    asyncio.run(notifier.check())
    assert api.sent == []


def test_notifier_fans_out_to_all_subscribed_chats(store):
    pid = store.create_project("yask")["id"]
    store.subscribe_project(-100, pid)  # a group chat
    store.subscribe_project(7, pid)
    api = FakeAPI()
    notifier = telegram_bot.Notifier(api, store)
    notifier.seed()
    t = _moved(store, pid)
    asyncio.run(notifier.check())
    assert api.sent == [
        (-100, _notification(pid, t, "working", "Review")),
        (7, _notification(pid, t, "working", "Review")),
    ]


def test_notifier_ignores_task_creation(store):
    pid = store.create_project("yask")["id"]
    store.subscribe_project(7, pid)
    api = FakeAPI()
    notifier = telegram_bot.Notifier(api, store)
    notifier.seed()
    store.create_task(pid, "brand new")
    asyncio.run(notifier.check())
    assert api.sent == []


def test_notifier_covers_archive_and_restore(store):
    pid = store.create_project("yask")["id"]
    store.subscribe_project(7, pid)
    api = FakeAPI()
    notifier = telegram_bot.Notifier(api, store)
    notifier.seed()
    t = store.create_task(pid, "doomed")
    store.archive_task(pid, t["number"], confirm=True)
    store.restore_task(pid, t["number"], confirm=True)
    asyncio.run(notifier.check())
    assert api.sent == [
        (7, _notification(pid, t, "doomed", "Archived")),
        (7, f"yask: #{t['number']} doomed — Archived → Backlog (/task {pid} {t['number']})"),
    ]


def test_notifier_failed_send_keeps_cursor_and_retries(store):
    pid = store.create_project("yask")["id"]
    store.subscribe_project(1, pid)
    store.subscribe_project(2, pid)
    api = FakeAPI(fail_for={2})
    notifier = telegram_bot.Notifier(api, store)
    notifier.seed()
    t = _moved(store, pid)
    expected = _notification(pid, t, "working", "Review")

    with pytest.raises(telegram_bot.BotAPIError):
        asyncio.run(notifier.check())
    # fan-out stopped at the failing chat; the cursor did not advance
    assert api.sent == [(1, expected)]
    # recovery: the pending change is re-sent; chat 1 gets a duplicate
    api.fail_for = set()
    asyncio.run(notifier.check())
    assert api.sent == [(1, expected), (1, expected), (2, expected)]


def test_run_bot_notifier_end_to_end(store):
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")

    # poll 1: /subscribe; poll 2: a second process (web UI / MCP) moves the
    # task, and /start arrives with the same batch; poll 3 drains the script
    script = Script(
        [
            [message_update(301, "/subscribe yask")],
            [message_update(302, "/start")],
        ]
    )
    base_handler = script.handler

    def handler(request):
        if request.url.path.endswith("/getUpdates") and len(script.offsets) == 1:
            store.move_task(pid, t["number"], "Review", confirm=True)
        return base_handler(request)

    script.stop = asyncio.Event()
    client = make_client(handler)
    api = telegram_bot.BotAPI(BOT_TOKEN, client=client)
    notifier = telegram_bot.Notifier(api, store)
    notifier.seed()

    async def go():
        try:
            await telegram_bot.run_bot(
                api,
                telegram_bot.make_dispatch(store),
                stop_event=script.stop,
                poll_timeout=1,
                error_delay=0.01,
                on_cycle=notifier.check,
            )
        finally:
            await client.aclose()

    asyncio.run(go())

    assert [m["chat_id"] for m in script.sent] == [7, 7, 7]
    assert script.sent[0]["text"] == (
        f"Subscribed to yask ({pid}) — you will be notified about "
        "task state changes in this project."
    )
    assert script.sent[1]["text"] == telegram_bot.START_TEXT
    # the notification arrives after the move, and the draining poll 3 adds
    # no duplicate
    assert script.sent[2]["text"] == _notification(pid, t, "working", "Review")
