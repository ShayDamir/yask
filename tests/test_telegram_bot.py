"""Tests for the Telegram bot process (``yask telegram``).

All Bot API traffic goes through ``httpx.MockTransport`` — real Telegram is
never called.
"""

import asyncio
import json
import time

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


def callback_update(update_id, data, chat_id=7, message_id=1):
    """A getUpdates payload entry carrying an inline-keyboard callback.

    The raw ``callback_query`` shape the bot routes to the callback
    dispatch: ``id``, ``data`` (the button payload), the original
    ``message`` (``message_id``/``chat.id``/``text``) and ``from``/
    ``chat_instance``.
    """
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cbq-{update_id}",
            "data": data,
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id},
                "text": "the message the button lives in",
            },
            "from": {"id": 42, "is_bot": False},
            "chat_instance": "ci-1",
        },
    }


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
    ``sent`` records sendMessage bodies; ``sent_files`` records multipart
    file uploads (sendDocument/sendPhoto): one dict per upload with the
    method, chat id, caption, filename, bytes and the ``reply_markup`` form
    field (None when absent); ``answered`` records answerCallbackQuery
    bodies; ``edited`` records editMessageText bodies; ``allowed_updates``
    records the allowed_updates of every getUpdates call; ``offsets`` the
    getUpdates offsets.
    """

    def __init__(self, get_updates, get_me_ok=True, fail_once_with=None):
        self.get_updates = list(get_updates)
        self.get_me_ok = get_me_ok
        self.fail_once_with = fail_once_with
        self.failed_once = False
        self.sent = []
        self.sent_files = []
        self.answered = []
        self.edited = []
        self.allowed_updates = []
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
                    "reply_markup": fields.get("reply_markup"),
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
            self.allowed_updates.append(body.get("allowed_updates"))
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
        if method == "answerCallbackQuery":
            self.answered.append(body)
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "editMessageText":
            self.edited.append(body)
            return httpx.Response(200, json={"ok": True, "result": True})
        raise AssertionError(f"unexpected Bot API method: {method}")


def run_bot_until_stop(
    script, dispatch=None, on_cycle=None, error_delay=0.01, callback_dispatch=None
):
    """Run run_bot against the script until the script drains (sets stop).

    ``dispatch`` defaults to the static ``reply_for`` (wrapped for the
    two-argument dispatch signature); pass a ``make_dispatch(store)``
    dispatcher for store-backed commands. ``on_cycle`` is passed through to
    ``run_bot`` (the state-change notifier hook); ``callback_dispatch`` the
    callback_query dispatcher (None → the out-of-date toast safety net).
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
                callback_dispatch=callback_dispatch,
            )
        finally:
            await client.aclose()

    asyncio.run(go())
    return script


def bot_api_call(script, calls):
    """Run BotAPI method calls directly against the script's mock transport.

    ``calls(api)`` is an async callable (the method invocations under
    test); the client is always closed. For the BotAPI-level tests that
    don't need the poll loop.
    """
    client = make_client(script.handler)
    api = telegram_bot.BotAPI(BOT_TOKEN, client=client)

    async def go():
        try:
            await calls(api)
        finally:
            await client.aclose()

    asyncio.run(go())


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


def test_stop_event_interrupts_in_flight_poll():
    """A set stop_event ends the bot mid long-poll, not after it returns.

    The mock getUpdates handler holds the request (a 60s server-side poll
    hold), so without the fix run_bot stays inside the in-flight poll until
    the handler unwinds. The stop event is set while the poll is in flight;
    the bot task must end within 10s (the unfixed code hangs until the
    60s hold expires, and the fixed one returns in milliseconds).
    """
    poll_in_flight = asyncio.Event()
    seen = []

    async def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        seen.append(method)
        if method == "getUpdates":
            poll_in_flight.set()
            await asyncio.sleep(60)  # the server-side long-poll hold
            return httpx.Response(200, json={"ok": True, "result": []})
        raise AssertionError(f"unexpected Bot API method: {method}")

    client = make_client(handler)
    api = telegram_bot.BotAPI(BOT_TOKEN, client=client)
    stop = asyncio.Event()

    def dispatch(text, chat_id=None):
        return telegram_bot.reply_for(text)

    async def go():
        bot_task = asyncio.ensure_future(
            telegram_bot.run_bot(
                api, dispatch, stop_event=stop, poll_timeout=30
            )
        )
        try:
            await poll_in_flight.wait()
            t0 = time.monotonic()
            stop.set()
            await asyncio.wait_for(bot_task, timeout=10)
            return time.monotonic() - t0
        finally:
            if not bot_task.done():
                bot_task.cancel()
                try:
                    await bot_task
                except asyncio.CancelledError:
                    pass
            await client.aclose()

    elapsed = asyncio.run(go())
    assert elapsed < 5
    # exactly one getUpdates was issued and nothing was sent (no replies,
    # no notifications)
    assert seen == ["getUpdates"]


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
    # no projects → no keyboard (the Bot API rejects an empty inline_keyboard)
    assert "reply_markup" not in script.sent[0]


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
    # one button per project, in display order (the p: callback payloads)
    assert script.sent[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "side-project", "callback_data": f"p:{side}"}],
            [{"text": "yask", "callback_data": f"p:{yask}"}],
            [{"text": "zeta", "callback_data": f"p:{zeta}"}],
        ]
    }
    # every payload is well under the Bot API's 64-byte callback_data limit
    for row in script.sent[0]["reply_markup"]["inline_keyboard"]:
        assert len(row[0]["callback_data"].encode("utf-8")) < 64


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


def test_projects_button_opens_tasks_view(store):
    """Pressing a /projects button must open the project's /tasks view."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    store.move_task(pid, t["number"], "In progress", confirm=True)
    # the button exactly as /projects emits it (the cross-task contract):
    # label = project name, payload "p:<pid>"
    first = run_bot_until_stop(
        Script([[message_update(93, "/projects")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    rows = first.sent[0]["reply_markup"]["inline_keyboard"]
    assert rows == [[{"text": "yask", "callback_data": f"p:{pid}"}]]
    payload = rows[0][0]["callback_data"]
    script = run_bot_until_stop(
        Script([[callback_update(94, payload, chat_id=11)]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the press is answered (no toast) and the project's /tasks view goes
    # out as a new message to the button's chat — the exact same view
    # typing "/tasks <id>" would send (text lines plus the t: keyboard)
    assert script.answered == [{"callback_query_id": "cbq-94"}]
    assert len(script.sent) == 1
    expected = telegram_bot.tasks_view(store, str(pid))
    assert script.sent[0]["chat_id"] == 11
    assert script.sent[0]["text"] == expected.text
    assert script.sent[0]["reply_markup"] == expected.reply_markup
    assert script.edited == []


def test_projects_button_no_active_tasks(store):
    """A project with only Backlog tasks answers with plain (none) text."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "backlog only")
    first = run_bot_until_stop(
        Script([[message_update(95, "/projects")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    payload = first.sent[0]["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    script = run_bot_until_stop(
        Script([[callback_update(96, payload, chat_id=11)]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert script.answered == [{"callback_query_id": "cbq-96"}]
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 11
    # no active tasks → plain text, no keyboard
    assert script.sent[0]["text"] == "Tasks in progress:\n(none)"
    assert "reply_markup" not in script.sent[0]
    assert script.edited == []


def test_callback_p_store_failure_replies_and_recovers(store, monkeypatch):
    """A store failure on a p: press replies with the error text, no crash."""
    pid = store.create_project("yask")["id"]

    def boom(project_id):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "get_project", boom)
    script = run_bot_until_stop(
        Script(
            [[callback_update(321, f"p:{pid}", chat_id=11), message_update(322, "/start")]]
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the press is answered (no toast), the failure produces an error reply,
    # and the loop survives: the follow-up message is still answered
    assert script.answered == [{"callback_query_id": "cbq-321"}]
    assert len(script.sent) == 2
    assert script.sent[0]["chat_id"] == 11
    assert script.sent[0]["text"] == telegram_bot.TASKS_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT
    assert script.offsets == [None, 323]


# --- /tasks (store-backed dispatch) ----------------------------------------


def test_tasks_empty_board(store):
    script = run_bot_until_stop(
        Script([[message_update(141, "/tasks")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == "Tasks in progress:\n(none)"
    # no tasks → no keyboard (the Bot API rejects an empty inline_keyboard)
    assert "reply_markup" not in script.sent[0]


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
    # exact text: name order, state grouping/order, id prefixes, and the
    # excluded states absent
    assert script.sent[0]["text"] == (
        "Tasks in progress:\n"
        f"{alpha}. alpha\n"
        "  Todo:\n"
        f"    #2 todo 1\n"
        "  Planning:\n"
        f"    #3 planning 1\n"
        "  In progress:\n"
        f"    #4 working\n"
        "  Review:\n"
        f"    #5 review 1\n"
        f"{zeta}. zeta\n"
        "  In progress:\n"
        f"    #1 z working"
    )
    # one button per task, in reading order (the t: callback payloads)
    assert script.sent[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#2 todo 1", "callback_data": f"t:{alpha}:2"}],
            [{"text": "#3 planning 1", "callback_data": f"t:{alpha}:3"}],
            [{"text": "#4 working", "callback_data": f"t:{alpha}:4"}],
            [{"text": "#5 review 1", "callback_data": f"t:{alpha}:5"}],
            [{"text": "#1 z working", "callback_data": f"t:{zeta}:1"}],
        ]
    }


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
        f"    #1 alpha working"
    )
    assert script.sent[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 alpha working", "callback_data": f"t:{alpha}:1"}]
        ]
    }

    # by project id
    script = run_bot_until_stop(
        Script([[message_update(112, f"/tasks {zeta}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Tasks in progress:\n"
        f"{zeta}. zeta\n"
        "  In progress:\n"
        f"    #1 zeta working"
    )
    assert script.sent[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 zeta working", "callback_data": f"t:{zeta}:1"}]
        ]
    }


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
        f"    #1 working"
    )
    assert script.sent[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 working", "callback_data": f"t:{pid}:1"}]
        ]
    }


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
    # no tasks → no keyboard (the Bot API rejects an empty inline_keyboard)
    assert "reply_markup" not in script.sent[0]


def test_tasks_unknown_project(store):
    store.create_project("alpha")
    script = run_bot_until_stop(
        Script([[message_update(131, "/tasks nope")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project 'nope' not found. Use /projects to list projects."
    )
    assert "reply_markup" not in script.sent[0]
    # unknown by id
    script = run_bot_until_stop(
        Script([[message_update(132, "/tasks 999")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project '999' not found. Use /projects to list projects."
    )
    assert "reply_markup" not in script.sent[0]


def test_tasks_with_bot_mention(store):
    script = run_bot_until_stop(
        Script([[message_update(151, "/tasks@yask_test_bot")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == "Tasks in progress:\n(none)"
    assert "reply_markup" not in script.sent[0]


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
    assert "reply_markup" not in script.sent[0]
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
    """Pressing a /tasks button must open the task's detail view."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(
        pid, "drill down", "Story", estimate=2.0, description="the point"
    )
    store.move_task(pid, t["number"], "In progress", confirm=True)
    # the button exactly as /tasks emits it (the cross-task contract):
    # label "#<n> <title>", payload "t:<pid>:<n>"
    first = run_bot_until_stop(
        Script([[message_update(248, "/tasks")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    rows = first.sent[0]["reply_markup"]["inline_keyboard"]
    assert rows == [
        [
            {
                "text": f"#{t['number']} drill down",
                "callback_data": f"t:{pid}:{t['number']}",
            }
        ]
    ]
    payload = rows[0][0]["callback_data"]
    script = run_bot_until_stop(
        Script([[callback_update(249, payload, chat_id=11)]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the press is answered (no toast) and the detail view goes out as a
    # new message to the button's chat
    assert script.answered == [{"callback_query_id": "cbq-249"}]
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 11
    assert script.sent[0]["text"].startswith(
        f"#{t['number']} drill down — Story\n"
    )
    assert "State: In progress" in script.sent[0]["text"]
    assert "Estimate: 2" in script.sent[0]["text"]
    assert script.edited == []


def test_task_view_attachment_and_toggle_buttons(store):
    """The /task detail keyboard: one row per attachment + the toggle."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    n = t["number"]
    attachment_rows = [
        [{"text": "plan.md", "callback_data": f"a:{pid}:{n}:{d['plan']['id']}"}],
        [{"text": "img.png", "callback_data": f"a:{pid}:{n}:{d['img']['id']}"}],
    ]
    script = run_bot_until_stop(
        Script([[message_update(281, f"/task yask {n}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    sent = script.sent[0]
    # the text is unchanged from the no-button form
    assert sent["text"] == expected_task_text(store, d)
    assert sent["reply_markup"] == {
        "inline_keyboard": attachment_rows
        + [[{"text": "Subscribe", "callback_data": f"s:{pid}"}]]
    }
    # subscribing the chat flips only the toggle button
    store.subscribe_project(7, pid)
    script = run_bot_until_stop(
        Script([[message_update(282, f"/task yask {n}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    rows = script.sent[0]["reply_markup"]["inline_keyboard"]
    assert rows[:2] == attachment_rows
    assert rows[-1] == [{"text": "Unsubscribe", "callback_data": f"u:{pid}"}]


def test_format_task_view_without_chat_is_plain_str(store):
    """No chat id → the plain text form, byte-identical (no keyboard)."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    reply = telegram_bot.format_task_view(
        store.get_task(pid, t["number"]),
        store.get_project(pid),
        store.get_history(pid, t["number"]),
    )
    assert isinstance(reply, str)
    assert reply == expected_task_text(store, d)


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

    async def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.fail_for:
            raise telegram_bot.BotAPIError("simulated send failure")
        self.sent.append((chat_id, text, reply_markup))
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


def _notification_markup(pid, t, title):
    """The single-button keyboard a notification message carries."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"#{t['number']} {title}",
                    "callback_data": f"t:{pid}:{t['number']}",
                }
            ]
        ]
    }


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
    markup = _notification_markup(pid, t, "working")
    asyncio.run(notifier.check())
    assert api.sent == [(7, _notification(pid, t, "working", "Review"), markup)]
    # a second cycle sends nothing new
    asyncio.run(notifier.check())
    assert api.sent == [(7, _notification(pid, t, "working", "Review"), markup)]


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
    markup = _notification_markup(pid, t, "working")
    asyncio.run(notifier.check())
    assert api.sent == [
        (-100, _notification(pid, t, "working", "Review"), markup),
        (7, _notification(pid, t, "working", "Review"), markup),
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
    markup = _notification_markup(pid, t, "doomed")
    asyncio.run(notifier.check())
    assert api.sent == [
        (7, _notification(pid, t, "doomed", "Archived"), markup),
        (
            7,
            f"yask: #{t['number']} doomed — Archived → Backlog (/task {pid} {t['number']})",
            markup,
        ),
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
    markup = _notification_markup(pid, t, "working")

    with pytest.raises(telegram_bot.BotAPIError):
        asyncio.run(notifier.check())
    # fan-out stopped at the failing chat; the cursor did not advance
    assert api.sent == [(1, expected, markup)]
    # recovery: the pending change is re-sent; chat 1 gets a duplicate
    api.fail_for = set()
    asyncio.run(notifier.check())
    assert api.sent == [
        (1, expected, markup),
        (1, expected, markup),
        (2, expected, markup),
    ]


def test_notification_message_carries_task_button(store):
    pid = store.create_project("yask")["id"]
    store.subscribe_project(7, pid)
    api = FakeAPI()
    notifier = telegram_bot.Notifier(api, store)
    notifier.seed()
    t = _moved(store, pid)
    asyncio.run(notifier.check())
    chat_id, text, markup = api.sent[0]
    assert chat_id == 7
    # the message text is unchanged; the button opens the task's detail view
    assert text == _notification(pid, t, "working", "Review")
    assert markup == {
        "inline_keyboard": [
            [
                {
                    "text": f"#{t['number']} working",
                    "callback_data": f"t:{pid}:{t['number']}",
                }
            ]
        ]
    }


def test_notification_button_opens_task_detail(store):
    pid = store.create_project("yask")["id"]
    store.subscribe_project(7, pid)
    api = FakeAPI()
    notifier = telegram_bot.Notifier(api, store)
    notifier.seed()
    t = _moved(store, pid)
    asyncio.run(notifier.check())
    # press the button the notification sent
    _, _, markup = api.sent[0]
    button = markup["inline_keyboard"][0][0]
    assert button["callback_data"] == f"t:{pid}:{t['number']}"
    action = telegram_bot.make_callback_dispatch(store)(
        callback_update(401, button["callback_data"])["callback_query"]
    )
    # chat 7 is subscribed, so the detail view carries the toggle in its
    # Unsubscribe state
    assert action is not None
    assert action.reply == telegram_bot.format_task_view(
        store.get_task(pid, t["number"]),
        store.get_project(pid),
        store.get_history(pid, t["number"]),
        chat_id=7,
        subscribed=True,
    )


def test_notification_button_label_truncated(store):
    long_title = "x" * 100  # well over the button label cap
    pid = store.create_project("yask")["id"]
    store.subscribe_project(7, pid)
    api = FakeAPI()
    notifier = telegram_bot.Notifier(api, store)
    notifier.seed()
    t = _moved(store, pid, long_title)
    asyncio.run(notifier.check())
    _, text, markup = api.sent[0]
    # the message text keeps the full title
    assert text == _notification(pid, t, long_title, "Review")
    button = markup["inline_keyboard"][0][0]
    cap = telegram_bot.NOTIFICATION_BUTTON_TEXT_MAX
    full = f"#{t['number']} {long_title}"
    # the label is cut to cap - 1 chars + ellipsis (total length == cap)
    assert len(full) > cap
    assert button["text"] == full[: cap - 1] + "…"
    assert len(button["text"]) == cap
    # the payload is unaffected by the truncation
    assert button["callback_data"] == f"t:{pid}:{t['number']}"


def test_run_bot_notifier_end_to_end(store):
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")

    # poll 1: /subscribe; poll 2: a second process (web UI / MCP) moves the
    # task, and /start arrives with the same batch; poll 3 presses the
    # notification's task button; poll 4 drains the script
    script = Script(
        [
            [message_update(301, "/subscribe yask")],
            [message_update(302, "/start")],
            [callback_update(303, f"t:{pid}:{t['number']}", chat_id=7)],
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
                callback_dispatch=telegram_bot.make_callback_dispatch(store),
            )
        finally:
            await client.aclose()

    asyncio.run(go())

    assert [m["chat_id"] for m in script.sent] == [7, 7, 7, 7]
    assert script.sent[0]["text"] == (
        f"Subscribed to yask ({pid}) — you will be notified about "
        "task state changes in this project."
    )
    assert script.sent[1]["text"] == telegram_bot.START_TEXT
    # the notification arrives after the move with its task-detail button,
    # and the draining poll 4 adds no duplicate
    assert script.sent[2]["text"] == _notification(pid, t, "working", "Review")
    assert script.sent[2]["reply_markup"] == _notification_markup(
        pid, t, "working"
    )
    # poll 3's button press sends the task detail view through the real
    # (mocked) HTTP layer, with the view's own keyboard (chat 7 subscribed
    # → the toggle in its Unsubscribe state)
    expected_detail = telegram_bot.format_task_view(
        store.get_task(pid, t["number"]),
        store.get_project(pid),
        store.get_history(pid, t["number"]),
        chat_id=7,
        subscribed=True,
    )
    assert isinstance(expected_detail, telegram_bot.KeyboardReply)
    assert script.sent[3]["text"] == expected_detail.text
    assert script.sent[3]["reply_markup"] == expected_detail.reply_markup


# --- inline-keyboard callbacks (BotAPI + run_bot plumbing) -------------------


def test_get_updates_subscribes_to_callback_queries():
    script = run_bot_until_stop(Script([[message_update(311, "/start")]]))
    assert script.allowed_updates
    assert all(
        au == ["message", "callback_query"] for au in script.allowed_updates
    )


def test_answer_callback_query_minimal():
    script = Script([])
    bot_api_call(script, lambda api: api.answer_callback_query("cbq-1"))
    # no text/show_alert/cache_time keys when the options are left default
    assert script.answered == [{"callback_query_id": "cbq-1"}]


def test_answer_callback_query_full_params():
    script = Script([])

    async def calls(api):
        await api.answer_callback_query(
            "cbq-2", text="Done!", show_alert=True, cache_time=30
        )

    bot_api_call(script, calls)
    assert script.answered == [
        {
            "callback_query_id": "cbq-2",
            "text": "Done!",
            "show_alert": True,
            "cache_time": 30,
        }
    ]


def test_edit_message_text_params():
    markup = {"inline_keyboard": [[{"text": "off", "callback_data": "u:1"}]]}
    script = Script([])

    async def calls(api):
        await api.edit_message_text(7, 99, "new text", markup)
        await api.edit_message_text(7, 99, "plain")

    bot_api_call(script, calls)
    assert script.edited == [
        {
            "chat_id": 7,
            "message_id": 99,
            "text": "new text",
            "reply_markup": markup,
        },
        # no markup → no key
        {"chat_id": 7, "message_id": 99, "text": "plain"},
    ]


def test_send_message_reply_markup_threaded():
    markup = {"inline_keyboard": [[{"text": "go", "callback_data": "p:1"}]]}
    script = Script([])

    async def calls(api):
        await api.send_message(7, "hello")
        await api.send_message(7, "hello", reply_markup=markup)

    bot_api_call(script, calls)
    assert script.sent == [
        # regression: a plain send is byte-identical to before
        {"chat_id": 7, "text": "hello"},
        {"chat_id": 7, "text": "hello", "reply_markup": markup},
    ]


def test_send_document_photo_reply_markup_threaded():
    markup = {"inline_keyboard": [[{"text": "view", "callback_data": "a:1:1:2"}]]}
    script = Script([])

    async def calls(api):
        await api.send_document(
            7, "doc.md", b"x", "text/markdown", reply_markup=markup
        )
        await api.send_photo(
            7, "img.png", b"y", "image/png", caption="c", reply_markup=markup
        )
        await api.send_document(7, "plain.md", b"z", "text/markdown")

    bot_api_call(script, calls)
    assert len(script.sent_files) == 3
    doc, photo, plain = script.sent_files
    assert doc["method"] == "sendDocument"
    assert photo["method"] == "sendPhoto"
    # multipart form fields are strings: the markup is a JSON string that
    # round-trips back to the dict (a Python dict would be rejected)
    assert json.loads(doc["reply_markup"]) == markup
    assert json.loads(photo["reply_markup"]) == markup
    assert photo["caption"] == "c"
    # no markup → no key
    assert plain["reply_markup"] is None


def test_callback_query_routed_to_callback_dispatch():
    seen = []

    def callback_dispatch(callback_query):
        seen.append(callback_query)
        return telegram_bot.CallbackAction(answer_text="ok", reply="detail")

    script = run_bot_until_stop(
        Script([[callback_update(321, "t:1:4")]]),
        callback_dispatch=callback_dispatch,
    )
    # the raw update dict is passed through, untouched
    assert len(seen) == 1
    assert seen[0]["id"] == "cbq-321"
    assert seen[0]["data"] == "t:1:4"
    assert seen[0]["message"]["chat"]["id"] == 7
    assert script.answered == [{"callback_query_id": "cbq-321", "text": "ok"}]
    assert script.sent == [{"chat_id": 7, "text": "detail"}]
    assert script.edited == []
    assert script.sent_files == []
    assert script.offsets == [None, 322]


def test_callback_keyboard_reply():
    markup = {"inline_keyboard": [[{"text": "done", "callback_data": "d:1"}]]}

    def callback_dispatch(callback_query):
        return telegram_bot.CallbackAction(
            reply=telegram_bot.KeyboardReply("here is the task", markup)
        )

    script = run_bot_until_stop(
        Script([[callback_update(331, "p:1")]]),
        callback_dispatch=callback_dispatch,
    )
    assert script.answered == [{"callback_query_id": "cbq-331"}]
    assert script.sent == [
        {"chat_id": 7, "text": "here is the task", "reply_markup": markup}
    ]


def test_callback_file_reply():
    markup = {"inline_keyboard": [[{"text": "open", "callback_data": "a:1:1:1"}]]}

    def callback_dispatch(callback_query):
        return telegram_bot.CallbackAction(
            reply=telegram_bot.FileReply(
                "plan.md", b"# plan", "text/markdown", "the plan",
                reply_markup=markup,
            )
        )

    script = run_bot_until_stop(
        Script([[callback_update(341, "a:1:1:1")]]),
        callback_dispatch=callback_dispatch,
    )
    assert script.sent == []
    assert len(script.sent_files) == 1
    f = script.sent_files[0]
    assert f["method"] == "sendDocument"
    assert f["chat_id"] == 7
    assert f["data"] == b"# plan"
    assert f["caption"] == "the plan"
    assert json.loads(f["reply_markup"]) == markup


def test_callback_edit_in_place():
    markup = {"inline_keyboard": [[{"text": "unsub", "callback_data": "u:1"}]]}

    def callback_dispatch(callback_query):
        return telegram_bot.CallbackAction(
            answer_text="unsubscribed",
            edit=telegram_bot.MessageEdit("Subscribed: no", markup),
        )

    script = run_bot_until_stop(
        Script([[callback_update(351, "u:1", chat_id=11, message_id=77)]]),
        callback_dispatch=callback_dispatch,
    )
    assert script.answered == [
        {"callback_query_id": "cbq-351", "text": "unsubscribed"}
    ]
    assert script.edited == [
        {
            "chat_id": 11,
            "message_id": 77,
            "text": "Subscribed: no",
            "reply_markup": markup,
        }
    ]
    # an edit updates the original message — no new message is sent
    assert script.sent == []
    assert script.offsets == [None, 352]


def test_unknown_callback_data_gets_toast():
    # no callback dispatch at all: the toast safety net
    script = run_bot_until_stop(
        Script([[callback_update(361, "stale:payload")]]),
    )
    assert script.answered == [
        {
            "callback_query_id": "cbq-361",
            "text": telegram_bot.UNKNOWN_CALLBACK_TEXT,
        }
    ]
    assert script.sent == []
    assert script.edited == []
    assert script.offsets == [None, 362]

    # a dispatch that returns None (an unrecognized payload): the same net
    def callback_dispatch(callback_query):
        return None

    script = run_bot_until_stop(
        Script(
            [
                [callback_update(363, "unknown:payload")],
                [message_update(364, "/start")],
            ]
        ),
        callback_dispatch=callback_dispatch,
    )
    assert script.answered == [
        {
            "callback_query_id": "cbq-363",
            "text": telegram_bot.UNKNOWN_CALLBACK_TEXT,
        }
    ]
    # nothing is sent for the callback, and the loop survived it: the next
    # message is still answered
    assert [m["text"] for m in script.sent] == [telegram_bot.START_TEXT]
    assert script.offsets == [None, 364, 365]


def test_callback_api_error_survived():
    def callback_dispatch(callback_query):
        return telegram_bot.CallbackAction(answer_text="ok", reply="detail")

    script = Script(
        [
            [callback_update(371, "t:1:4")],
            [message_update(372, "/start")],
        ]
    )
    base_handler = script.handler
    answer_calls = []

    def handler(request):
        if request.url.path.rsplit("/", 1)[-1] == "answerCallbackQuery":
            answer_calls.append(1)
            if len(answer_calls) == 1:
                # a plain ok:false body (no 429 retry semantics)
                return httpx.Response(
                    200,
                    json={
                        "ok": False,
                        "error_code": 400,
                        "description": "query is too old and response timeout expired",
                    },
                )
        return base_handler(request)

    script.stop = asyncio.Event()
    client = make_client(handler)
    api = telegram_bot.BotAPI(BOT_TOKEN, client=client)

    async def go():
        try:
            await telegram_bot.run_bot(
                api,
                lambda text, chat_id=None: telegram_bot.reply_for(text),
                stop_event=script.stop,
                poll_timeout=1,
                error_delay=0.01,
                callback_dispatch=callback_dispatch,
            )
        finally:
            await client.aclose()

    asyncio.run(go())

    # the failed answer was logged, not raised, and skipped the rest of the
    # callback handling (no reply sent) — mirroring test_transport_error_is_survived
    assert len(answer_calls) == 1
    assert [m["text"] for m in script.sent] == [telegram_bot.START_TEXT]
    # the offset still advanced past the failed callback
    assert script.offsets == [None, 372, 373]


def test_callback_missing_message_skips_edit():
    def callback_dispatch(callback_query):
        return telegram_bot.CallbackAction(
            answer_text="edited",
            edit=telegram_bot.MessageEdit("new text"),
        )

    # an old message arrives as a MaybeInaccessibleMessage: no message_id
    update = callback_update(381, "t:1:4")
    update["callback_query"]["message"] = {"chat": {"id": 7}}
    script = run_bot_until_stop(
        Script(
            [
                [update],
                [message_update(382, "/start")],
            ]
        ),
        callback_dispatch=callback_dispatch,
    )
    # the edit is skipped, the answer still goes out, the loop survives
    assert script.answered == [{"callback_query_id": "cbq-381", "text": "edited"}]
    assert script.edited == []
    assert [m["text"] for m in script.sent] == [telegram_bot.START_TEXT]
    assert script.offsets == [None, 382, 383]


def test_make_callback_dispatch_skeleton(store):
    # with the p:, t:, a: and s:/u: handlers: a well-formed ``p:`` payload
    # is handled (a fresh store has no projects → the not-found reply), an
    # unrecognised payload is still unhandled (the out-of-date toast
    # answers it), and a valid ``t:`` payload is handled — the callback's
    # message carries a chat, so the detail reply is a KeyboardReply with
    # the task view's own buttons (no attachments here → just the toggle).
    dispatch = telegram_bot.make_callback_dispatch(store)
    action = dispatch(callback_update(1, "p:1")["callback_query"])
    assert action is not None
    assert action.answer_text is None
    assert action.edit is None
    assert (
        action.reply
        == "Project '1' not found. Use /projects to list projects."
    )
    assert (
        dispatch(callback_update(3, "stale:payload")["callback_query"])
        is None
    )
    pid = store.create_project("alpha")["id"]
    t = store.create_task(pid, "working")
    # callback_update's message has chat.id == 7, so the detail reply is the
    # keyboard form for that (unsubscribed) chat
    action = dispatch(callback_update(2, f"t:{pid}:{t['number']}")["callback_query"])
    assert action is not None
    assert action.answer_text is None
    assert action.edit is None
    assert action.reply == telegram_bot.format_task_view(
        store.get_task(pid, t["number"]),
        store.get_project(pid),
        store.get_history(pid, t["number"]),
        chat_id=7,
        subscribed=False,
    )


def test_callback_dispatch_task_detail_round_trip(store):
    """A button press through run_bot: answered, detail sent, offset moves."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working", "Story", estimate=3.0)
    store.move_task(pid, t["number"], "In progress", confirm=True)
    script = run_bot_until_stop(
        Script([[callback_update(302, f"t:{pid}:{t['number']}", chat_id=11)]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # answered without a toast; the detail view goes out as a new message
    assert script.answered == [{"callback_query_id": "cbq-302"}]
    assert len(script.sent) == 1
    sent = script.sent[0]
    assert sent["chat_id"] == 11
    assert sent["text"].startswith(f"#{t['number']} working — Story\n")
    assert "State: In progress" in sent["text"]
    assert "Estimate: 3" in sent["text"]
    # the detail carries its own keyboard (no attachments on this task →
    # just the subscribe toggle, reflecting chat 11's subscription state)
    assert sent["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Subscribe", "callback_data": f"s:{pid}"}]
        ]
    }
    assert script.edited == []
    assert script.offsets == [None, 303]


def test_callback_dispatch_unknown_task(store):
    pid = store.create_project("alpha")["id"]
    dispatch = telegram_bot.make_callback_dispatch(store)
    action = dispatch(callback_update(303, f"t:{pid}:42")["callback_query"])
    assert action is not None
    assert action.answer_text is None
    assert action.edit is None
    assert action.reply == "Task #42 not found in alpha."


def test_callback_dispatch_unknown_project(store):
    store.create_project("alpha")
    dispatch = telegram_bot.make_callback_dispatch(store)
    action = dispatch(callback_update(304, "t:999:1")["callback_query"])
    assert action is not None
    assert action.answer_text is None
    assert action.edit is None
    assert (
        action.reply
        == "Project '999' not found. Use /projects to list projects."
    )


def test_callback_dispatch_unknown_project_p(store):
    store.create_project("alpha")
    dispatch = telegram_bot.make_callback_dispatch(store)
    action = dispatch(callback_update(308, "p:999")["callback_query"])
    assert action is not None
    assert action.answer_text is None
    assert action.edit is None
    assert (
        action.reply
        == "Project '999' not found. Use /projects to list projects."
    )


def test_callback_t_store_failure_replies_and_recovers(store, monkeypatch):
    """A store failure on a t: press replies with the error text, no crash."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working", "Story", estimate=3.0)
    store.move_task(pid, t["number"], "In progress", confirm=True)

    def boom(project_id, number):
        raise RuntimeError("simulated store failure")

    # get_history is the call the press reaches after the (succeeding)
    # project/task lookups — the press must not leak its failure
    monkeypatch.setattr(store, "get_history", boom)
    script = run_bot_until_stop(
        Script(
            [
                [
                    callback_update(323, f"t:{pid}:{t['number']}", chat_id=11),
                    message_update(324, "/start"),
                ]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the press is answered (no toast), the failure produces an error reply,
    # and the loop survives: the follow-up message is still answered
    assert script.answered == [{"callback_query_id": "cbq-323"}]
    assert len(script.sent) == 2
    assert script.sent[0]["chat_id"] == 11
    assert script.sent[0]["text"] == telegram_bot.TASK_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT
    assert script.offsets == [None, 325]


# --- a: payload (the /task view's per-attachment buttons) -------------------


def test_callback_dispatch_attachment_sends_file(store):
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    dispatch = telegram_bot.make_callback_dispatch(store)
    action = dispatch(
        callback_update(291, f"a:{pid}:{t['number']}:{d['plan']['id']}")[
            "callback_query"
        ]
    )
    assert action is not None
    assert action.answer_text is None
    assert action.edit is None
    reply = action.reply
    assert isinstance(reply, telegram_bot.FileReply)
    assert reply.filename == "plan.md"
    assert reply.data == d["plan_bytes"]
    assert reply.content_type == "text/markdown"
    assert reply.caption == f"#{t['number']} working — plan.md"


def test_callback_dispatch_attachment_round_trip(store):
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    # markdown attachment → sendDocument
    script = run_bot_until_stop(
        Script(
            [[callback_update(292, f"a:{pid}:{t['number']}:{d['plan']['id']}", chat_id=13)]]
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert script.answered == [{"callback_query_id": "cbq-292"}]
    assert script.sent == []  # a file, not a text message
    assert len(script.sent_files) == 1
    f = script.sent_files[0]
    assert f["method"] == "sendDocument"
    assert f["chat_id"] == 13
    assert f["filename"] == "plan.md"
    assert f["data"] == d["plan_bytes"]
    assert f["caption"] == f"#{t['number']} working — plan.md"
    # image attachment → sendPhoto (the /attachment convention)
    script = run_bot_until_stop(
        Script(
            [[callback_update(293, f"a:{pid}:{t['number']}:{d['img']['id']}", chat_id=13)]]
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert len(script.sent_files) == 1
    f = script.sent_files[0]
    assert f["method"] == "sendPhoto"
    assert f["filename"] == "img.png"
    assert f["data"] == PNG
    assert f["caption"] == f"#{t['number']} working — img.png"


def test_callback_dispatch_attachment_not_found(store):
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    dispatch = telegram_bot.make_callback_dispatch(store)
    # unknown project
    action = dispatch(callback_update(294, "a:999:1:1")["callback_query"])
    assert action is not None
    assert action.reply == (
        "Project '999' not found. Use /projects to list projects."
    )
    # unknown task
    action = dispatch(
        callback_update(295, f"a:{pid}:42:{d['plan']['id']}")["callback_query"]
    )
    assert action is not None
    assert action.reply == "Task #42 not found in yask."
    # unknown attachment id
    action = dispatch(
        callback_update(296, f"a:{pid}:{t['number']}:4242")["callback_query"]
    )
    assert action is not None
    assert action.reply == (
        f"Attachment 4242 not found on task #{t['number']} (yask). "
        f"Use /task yask {t['number']} to list the task's attachments."
    )
    # a foreign attachment (belongs to another task) never leaks
    other = store.create_task(pid, "other")
    foreign = store.add_attachment(
        pid, other["number"], "x.md", "text/markdown", b"x"
    )
    action = dispatch(
        callback_update(297, f"a:{pid}:{t['number']}:{foreign['id']}")[
            "callback_query"
        ]
    )
    assert action is not None
    assert action.reply == (
        f"Attachment {foreign['id']} not found on task #{t['number']} (yask). "
        f"Use /task yask {t['number']} to list the task's attachments."
    )


def test_callback_a_store_failure_replies_and_recovers(store, monkeypatch):
    """A store failure on an a: press replies with the error text, no crash."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]

    def boom(project_id, number, attachment_id):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "get_task_attachment", boom)
    script = run_bot_until_stop(
        Script(
            [
                [
                    callback_update(
                        325, f"a:{pid}:{t['number']}:{d['plan']['id']}", chat_id=13
                    ),
                    message_update(326, "/start"),
                ]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the press is answered (no toast), the failure produces an error reply
    # (no file is sent), and the loop survives
    assert script.answered == [{"callback_query_id": "cbq-325"}]
    assert len(script.sent) == 2
    assert script.sent[0]["chat_id"] == 13
    assert script.sent[0]["text"] == telegram_bot.ATTACHMENT_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT
    assert script.sent_files == []
    assert script.offsets == [None, 327]


# --- s:/u: payload (the /task view's subscribe toggle) ----------------------


def _detail_callback(update_id, data, chat_id, detail):
    """A callback_update whose original message carries the /task detail
    (``text`` + ``reply_markup``) — the shape Telegram sends when a button
    on the detail message is pressed."""
    update = callback_update(update_id, data, chat_id=chat_id)
    update["callback_query"]["message"]["text"] = detail["text"]
    update["callback_query"]["message"]["reply_markup"] = detail["reply_markup"]
    return update


def test_callback_dispatch_subscribe_toggle(store):
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    chat_id = 21
    # the real /task detail (text + keyboard) as the original message
    first = run_bot_until_stop(
        Script([[message_update(298, f"/task yask {t['number']}", chat_id=chat_id)]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    detail = first.sent[0]
    update = _detail_callback(299, f"s:{pid}", chat_id, detail)
    script = run_bot_until_stop(
        Script([[update]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the store row is created
    assert [s["project_id"] for s in store.list_subscriptions(chat_id)] == [pid]
    # the press is answered with a toast; no message is stacked
    assert script.answered == [
        {"callback_query_id": "cbq-299", "text": "Subscribed to yask"}
    ]
    assert script.sent == []
    # in-place edit: original text, attachment rows preserved, toggle flips
    assert len(script.edited) == 1
    e = script.edited[0]
    assert e["chat_id"] == chat_id
    assert e["message_id"] == 1
    assert e["text"] == detail["text"]
    rows = e["reply_markup"]["inline_keyboard"]
    assert rows[:2] == detail["reply_markup"]["inline_keyboard"][:2]
    assert rows[-1] == [{"text": "Unsubscribe", "callback_data": f"u:{pid}"}]


def test_callback_dispatch_unsubscribe_toggle(store):
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    chat_id = 22
    store.subscribe_project(chat_id, pid)
    first = run_bot_until_stop(
        Script([[message_update(300, f"/task yask {t['number']}", chat_id=chat_id)]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    detail = first.sent[0]
    # the detail shows Unsubscribe for the subscribed chat
    assert detail["reply_markup"]["inline_keyboard"][-1] == [
        {"text": "Unsubscribe", "callback_data": f"u:{pid}"}
    ]
    update = _detail_callback(301, f"u:{pid}", chat_id, detail)
    script = run_bot_until_stop(
        Script([[update]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the store row is removed
    assert [s["project_id"] for s in store.list_subscriptions(chat_id)] == []
    assert script.answered == [
        {"callback_query_id": "cbq-301", "text": "Unsubscribed from yask"}
    ]
    assert script.sent == []
    e = script.edited[0]
    rows = e["reply_markup"]["inline_keyboard"]
    assert rows[:2] == detail["reply_markup"]["inline_keyboard"][:2]
    assert rows[-1] == [{"text": "Subscribe", "callback_data": f"s:{pid}"}]


def test_callback_s_store_failure_replies_and_recovers(store, monkeypatch):
    """A store failure on an s: press replies with the error text, no crash."""
    pid = store.create_project("yask")["id"]

    def boom(chat_id, project_id):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "subscribe_project", boom)
    script = run_bot_until_stop(
        Script(
            [
                [
                    callback_update(327, f"s:{pid}", chat_id=11),
                    message_update(328, "/start"),
                ]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the press is answered (no toast), the failure produces an error reply
    # (no in-place edit), and the loop survives
    assert script.answered == [{"callback_query_id": "cbq-327"}]
    assert len(script.sent) == 2
    assert script.sent[0]["chat_id"] == 11
    assert script.sent[0]["text"] == telegram_bot.SUBSCRIBE_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT
    assert store.list_subscriptions(11) == []
    assert script.edited == []
    assert script.offsets == [None, 329]


def test_callback_u_store_failure_replies_and_recovers(store, monkeypatch):
    """A store failure on a u: press replies with the error text, no crash."""
    pid = store.create_project("yask")["id"]
    store.subscribe_project(11, pid)

    def boom(chat_id, project_id):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "unsubscribe_project", boom)
    script = run_bot_until_stop(
        Script(
            [
                [
                    callback_update(329, f"u:{pid}", chat_id=11),
                    message_update(330, "/start"),
                ]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the press is answered (no toast), the failure produces an error reply
    # (no in-place edit), and the loop survives
    assert script.answered == [{"callback_query_id": "cbq-329"}]
    assert len(script.sent) == 2
    assert script.sent[0]["chat_id"] == 11
    assert script.sent[0]["text"] == telegram_bot.UNSUBSCRIBE_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT
    # the failed unsubscribe leaves the store row in place
    assert [s["project_id"] for s in store.list_subscriptions(11)] == [pid]
    assert script.edited == []
    assert script.offsets == [None, 331]


def test_callback_dispatch_toggle_stale_keyboard(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "working")
    dispatch = telegram_bot.make_callback_dispatch(store)
    # (a) no reply_markup on the original message → a single toggle row
    action = dispatch(callback_update(302, f"s:{pid}", chat_id=23)["callback_query"])
    assert action is not None
    assert action.answer_text == "Subscribed to yask"
    assert action.edit.text == "the message the button lives in"
    assert action.edit.reply_markup == {
        "inline_keyboard": [[{"text": "Unsubscribe", "callback_data": f"u:{pid}"}]]
    }
    # (b) a keyboard without the toggle row (stale layout) → single toggle
    update = callback_update(303, f"u:{pid}", chat_id=24)
    update["callback_query"]["message"]["reply_markup"] = {
        "inline_keyboard": [[{"text": "plan.md", "callback_data": f"a:{pid}:1:1"}]]
    }
    action = dispatch(update["callback_query"])
    assert action is not None
    assert action.answer_text == "Unsubscribed from yask"
    assert action.edit.reply_markup == {
        "inline_keyboard": [[{"text": "Subscribe", "callback_data": f"s:{pid}"}]]
    }


def test_callback_dispatch_toggle_unknown_project(store):
    store.create_project("yask")
    dispatch = telegram_bot.make_callback_dispatch(store)
    action = dispatch(callback_update(304, "s:999", chat_id=25)["callback_query"])
    assert action is not None
    assert action.answer_text is None
    assert action.edit is None
    assert action.reply == (
        "Project '999' not found. Use /projects to list projects."
    )
    # no store change
    assert store.list_subscriptions(25) == []


def test_callback_dispatch_toggle_inaccessible_message(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "working")
    update = callback_update(305, f"s:{pid}", chat_id=26)
    # an old message arrives as a MaybeInaccessibleMessage: no message_id
    # and no text, but the chat is still there
    update["callback_query"]["message"] = {"chat": {"id": 26}}
    script = run_bot_until_stop(
        Script([[update]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the subscription is still toggled and toasted, the edit is skipped
    assert [s["project_id"] for s in store.list_subscriptions(26)] == [pid]
    assert script.answered == [
        {"callback_query_id": "cbq-305", "text": "Subscribed to yask"}
    ]
    assert script.edited == []
    assert script.sent == []


@pytest.mark.parametrize(
    "payload",
    [
        "t:1", "t:1:4:9", "t:x:4", "t:", "p:", "p:abc", "stale:payload",
        "a:1", "a:1:2", "a:1:2:3:4:5", "a:x:1:2", "a:1:x:2", "a:1:2:x",
        "s:", "s:abc", "s:1:2", "u:", "u:abc", "u:1:2",
    ],
)
def test_callback_dispatch_unhandled_payloads(store, payload):
    # wrong family, wrong arity, or non-numeric fields: nothing to do →
    # run_bot answers with the out-of-date toast
    dispatch = telegram_bot.make_callback_dispatch(store)
    assert dispatch(callback_update(305, payload)["callback_query"]) is None


def test_callback_dispatch_unhandled_payload_toast(store):
    store.create_project("alpha")
    script = run_bot_until_stop(
        Script(
            [
                [callback_update(306, "t:x:4")],
                [message_update(307, "/start")],
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert script.answered == [
        {
            "callback_query_id": "cbq-306",
            "text": telegram_bot.UNKNOWN_CALLBACK_TEXT,
        }
    ]
    # nothing is sent for the callback; the loop survives (next message OK)
    assert [m["text"] for m in script.sent] == [telegram_bot.START_TEXT]
    assert script.offsets == [None, 307, 308]
