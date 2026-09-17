"""Tests for the Telegram bot process (``yask telegram``).

All Bot API traffic goes through ``httpx.MockTransport`` — real Telegram is
never called.
"""

import asyncio
import html
import json
import re
import sqlite3
import time

import httpx
import pytest

from yask import cli
from yask import db
from yask import telegram_bot
from yask.store import Store

BOT_TOKEN = "12345:TEST"


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def message_update(update_id, text, chat_id=7):
    """A getUpdates payload entry carrying a message (text may be None)."""
    message = {"message_id": 1, "chat": {"id": chat_id}}
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def file_message_update(
    update_id, document=None, photo=None, caption=None, chat_id=7
):
    """A getUpdates payload entry carrying a file message (the ``/attach``
    channel): a ``document`` (file_id/file_name/mime_type/file_size) or a
    ``photo`` (a list of PhotoSizes), with the command channel riding in
    the ``caption`` (Telegram file messages have no ``text``)."""
    message = {"message_id": 1, "chat": {"id": chat_id}}
    if document is not None:
        message["document"] = document
    if photo is not None:
        message["photo"] = photo
    if caption is not None:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


def document_update(
    update_id,
    caption=None,
    chat_id=7,
    file_name="notes.md",
    mime_type="text/markdown",
    file_size=10,
    file_id="DOC-1",
):
    """A document message with sensible defaults (a small markdown file)."""
    return file_message_update(
        update_id,
        document={
            "file_id": file_id,
            "file_name": file_name,
            "mime_type": mime_type,
            "file_size": file_size,
        },
        caption=caption,
        chat_id=chat_id,
    )


def photo_update(update_id, caption=None, chat_id=7):
    """A photo message with three PhotoSizes (the bot must take the
    largest — the last element's ``file_id``)."""
    return file_message_update(
        update_id,
        photo=[
            {"file_id": "PHOTO-S", "file_size": 100},
            {"file_id": "PHOTO-M", "file_size": 1000},
            {"file_id": "PHOTO-L", "file_size": 10000},
        ],
        caption=caption,
        chat_id=chat_id,
    )


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
    ``sent`` records sendMessage bodies; ``sent_rich`` records
    sendRichMessage bodies; ``sent_files`` records multipart
    file uploads (sendDocument/sendPhoto): one dict per upload with the
    method, chat id, caption, filename, bytes and the ``reply_markup`` form
    field (None when absent); ``answered`` records answerCallbackQuery
    bodies; ``edited`` records editMessageText bodies; ``command_requests``
    records (method, body) tuples for setMyCommands / getMyCommands /
    deleteMyCommands; ``allowed_updates`` records the allowed_updates of every
    getUpdates call; ``offsets`` the getUpdates offsets. ``file_gets``
    records the file ids passed to getFile; ``file_downloads`` the URL paths
    requested from the file CDN (``/file/bot<token>/<path>``), served with
    the ``file_bytes`` argument (a 404 when it is None).
    ``fail_rich_once`` / ``fail_send_once`` / ``fail_edit_once`` each take
    a ``(status, description)`` pair: the first sendRichMessage /
    sendMessage / editMessageText is then answered ``ok:false`` with that
    ``error_code`` (a simulated rich-leg, HTML-leg or in-place-edit
    failure), subsequent calls succeed.
    """

    def __init__(self, get_updates, get_me_ok=True, fail_once_with=None, fail_set_my_commands=False, file_bytes=None, fail_rich_once=None, fail_send_once=None, fail_edit_once=None):
        self.get_updates = list(get_updates)
        self.get_me_ok = get_me_ok
        self.fail_once_with = fail_once_with
        self.failed_once = False
        self.fail_set_my_commands = fail_set_my_commands
        self.failed_set_my_commands = False
        self.fail_rich_once = fail_rich_once
        self.failed_rich_once = False
        self.fail_send_once = fail_send_once
        self.failed_send_once = False
        self.fail_edit_once = fail_edit_once
        self.failed_edit_once = False
        self.file_bytes = file_bytes
        self.file_gets = []
        self.file_downloads = []
        self.sent = []
        self.sent_rich = []
        self.sent_files = []
        self.answered = []
        self.edited = []
        self.command_requests = []
        self.allowed_updates = []
        self.offsets = []
        self.stop = None

    def handler(self, request):
        if self.fail_once_with is not None and not self.failed_once:
            self.failed_once = True
            raise self.fail_once_with
        # The file CDN: a plain GET of /file/bot<token>/<path> (no JSON
        # body), served with file_bytes (a 404 when none was configured).
        if request.url.path.startswith("/file/"):
            self.file_downloads.append(request.url.path)
            if self.file_bytes is None:
                return httpx.Response(404, text="no file configured")
            return httpx.Response(200, content=self.file_bytes)
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getFile":
            body = json.loads(request.content)
            self.file_gets.append(body.get("file_id"))
            file_size = len(self.file_bytes) if self.file_bytes is not None else 10
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "file_id": body.get("file_id"),
                        "file_unique_id": "UNIQ-1",
                        "file_size": file_size,
                        "file_path": "cdn/path/FILE-1",
                    },
                },
            )
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
            if self.fail_send_once is not None and not self.failed_send_once:
                self.failed_send_once = True
                status, description = self.fail_send_once
                return httpx.Response(
                    status,
                    json={
                        "ok": False,
                        "error_code": status,
                        "description": description,
                    },
                )
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
        if method == "sendRichMessage":
            self.sent_rich.append(body)
            if self.fail_rich_once is not None and not self.failed_rich_once:
                self.failed_rich_once = True
                status, description = self.fail_rich_once
                return httpx.Response(
                    status,
                    json={
                        "ok": False,
                        "error_code": status,
                        "description": description,
                    },
                )
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
        if method == "answerCallbackQuery":
            self.answered.append(body)
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "editMessageText":
            self.edited.append(body)
            if self.fail_edit_once is not None and not self.failed_edit_once:
                self.failed_edit_once = True
                status, description = self.fail_edit_once
                return httpx.Response(
                    status,
                    json={
                        "ok": False,
                        "error_code": status,
                        "description": description,
                    },
                )
            return httpx.Response(200, json={"ok": True, "result": True})
        if method in ("setMyCommands", "getMyCommands", "deleteMyCommands"):
            self.command_requests.append((method, body))
            if (
                method == "setMyCommands"
                and self.fail_set_my_commands
                and not self.failed_set_my_commands
            ):
                self.failed_set_my_commands = True
                return httpx.Response(
                    500,
                    json={
                        "ok": False,
                        "error_code": 500,
                        "description": "telegram unreachable",
                    },
                )
            if method == "getMyCommands":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": [{"command": "/start", "description": "main"}],
                    },
                )
            return httpx.Response(200, json={"ok": True, "result": True})
        raise AssertionError(f"unexpected Bot API method: {method}")


def run_bot_until_stop(
    script, dispatch=None, on_cycle=None, error_delay=0.01, callback_dispatch=None,
    build_dispatch=None, rich=True,
):
    """Run run_bot against the script until the script drains (sets stop).

    ``dispatch`` defaults to the static ``reply_for`` (wrapped for the
    two-argument dispatch signature); pass a ``make_dispatch(store)``
    dispatcher for store-backed commands. ``on_cycle`` is passed through to
    ``run_bot`` (the state-change notifier hook); ``callback_dispatch`` the
    callback_query dispatcher (None → the out-of-date toast safety net);
    ``rich`` the kill-switch flag threaded to ``run_bot`` (default on).
    ``build_dispatch`` — when given — is a callback receiving the internal
    :class:`telegram_bot.BotAPI` instance (bound to the same mock
    transport) and returning the dispatch callable; it is how a test wires
    ``make_dispatch(store, auth, api=api)`` so the dispatch layer's async
    file handler downloads through the mock (the production wiring is
    ``_amain``'s ``make_dispatch(store, auth, api=api)``).
    """
    script.stop = asyncio.Event()
    if dispatch is None and build_dispatch is None:
        def dispatch(text, chat_id=None, file=None):
            return telegram_bot.reply_for(text)

    async def go():
        client = make_client(script.handler)
        api = telegram_bot.BotAPI(BOT_TOKEN, client=client)
        try:
            d = build_dispatch(api) if build_dispatch is not None else dispatch
            await telegram_bot.run_bot(
                api,
                d,
                stop_event=script.stop,
                poll_timeout=1,
                error_delay=error_delay,
                on_cycle=on_cycle,
                callback_dispatch=callback_dispatch,
                rich=rich,
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


def test_login_session_survives_a_bot_restart(tmp_path):
    """A /login in one process run stays authenticated in the next: the
    session is stored in the database, not in process memory."""
    # Seed: one permitted chat and one project, in the file the bot opens.
    conn = db.connect(tmp_path / "yask.db")
    seed = Store(conn)
    seed.add_telegram_user(7, "pw")
    seed.create_project("yask")
    conn.close()

    # Run 1: the chat logs in.
    script1 = Script([[message_update(711, "/login pw")]])
    script1.stop = asyncio.Event()
    code = telegram_bot.main(
        BOT_TOKEN, tmp_path, client=make_client(script1.handler),
        stop_event=script1.stop,
    )
    assert code == 0
    assert [m["text"] for m in script1.sent] == [telegram_bot.LOGIN_OK_TEXT]

    # Run 2 (the restart): the same chat is still authenticated —
    # /projects answers the real view, not the auth-required notice.
    script2 = Script([[message_update(712, "/projects")]])
    script2.stop = asyncio.Event()
    code = telegram_bot.main(
        BOT_TOKEN, tmp_path, client=make_client(script2.handler),
        stop_event=script2.stop,
    )
    assert code == 0
    # the populated board goes out rich (the list is a rich surface)
    assert len(script2.sent_rich) == 1
    assert script2.sent == []
    text = script2.sent_rich[0]["rich_message"]["markdown"]
    assert text != telegram_bot.AUTH_REQUIRED_TEXT
    assert text.startswith("# Projects")
    assert "yask" in text


def test_legacy_telegram_users_db_gets_authenticated_at_column(tmp_path):
    """A database predating the column is upgraded through the real ALTER
    path: the table is built with the old DDL by a raw connection, so
    connect() (not the schema) must add the column."""
    path = tmp_path / "yask.db"
    legacy = sqlite3.connect(str(path))
    try:
        legacy.execute(
            "CREATE TABLE telegram_users ("
            "chat_id INTEGER PRIMARY KEY, "
            "password_hash TEXT NOT NULL, "
            "created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        legacy.execute(
            "INSERT INTO telegram_users"
            "(chat_id, password_hash, created_at, updated_at) "
            "VALUES (7, 'scrypt$1$1$1$00$00', '2020-01-01T00:00:00Z', "
            "'2020-01-01T00:00:00Z')"
        )
        legacy.commit()
    finally:
        legacy.close()

    conn = db.connect(path)
    try:
        store = Store(conn)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(telegram_users)")}
        assert "authenticated_at" in cols
        row = conn.execute(
            "SELECT authenticated_at FROM telegram_users WHERE chat_id = 7"
        ).fetchone()
        assert row["authenticated_at"] is None  # legacy row: no session yet
        assert store.is_telegram_user_authenticated(7) is False
    finally:
        conn.close()


def test_start_command():
    script = run_bot_until_stop(Script([[message_update(101, "/start")]]))
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == telegram_bot.START_TEXT
    # the /start reply carries the hub's exact keyboard (the intro plus
    # the hub is the natural entry point, #62)
    assert script.sent[0]["reply_markup"] == telegram_bot.menu_view().reply_markup
    # offset advanced past the processed update
    assert script.offsets == [None, 102]


def test_start_renders_menu():
    """/start goes out with the intro plus the hub's exact two-row keyboard."""
    script = run_bot_until_stop(Script([[message_update(411, "/start")]]))
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == telegram_bot.START_TEXT
    # the exact two-row layout: Projects/Tasks, then Subscriptions/Add task/Help
    assert script.sent[0]["reply_markup"] == {
        "inline_keyboard": [
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
    }


def test_help_command():
    script = run_bot_until_stop(Script([[message_update(21, "/help")]]))
    assert len(script.sent) == 1
    text = script.sent[0]["text"]
    assert text == telegram_bot.HELP_TEXT
    assert "/start" in text
    assert "/help" in text
    # the /help reply carries the trailing Main-menu row (#62)
    assert script.sent[0]["reply_markup"] == {
        "inline_keyboard": [[{"text": "Main menu", "callback_data": "h"}]]
    }
    assert script.offsets == [None, 22]


def test_help_renders_reference_with_menu_button():
    """/help goes out with the full reference plus the Main-menu button."""
    script = run_bot_until_stop(Script([[message_update(412, "/help")]]))
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == telegram_bot.HELP_TEXT
    assert script.sent[0]["reply_markup"] == {
        "inline_keyboard": [[{"text": "Main menu", "callback_data": "h"}]]
    }


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

    def dispatch(text, chat_id=None, file=None):
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

    def dispatch(text, chat_id=None, file=None):
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


# --- token redaction (CWE-532) ---------------------------------------------
#
# The bot token rides in every request URL. Some httpx versions echo the
# request URL in their transport-error text; whichever version is resolved,
# the BotAPIError raised (and therefore printed) on a transport failure must
# never contain the token. The handler below simulates the URL-echoing
# behavior.


def _url_echoing_handler(request):
    """A MockTransport handler failing like an httpx version whose error
    text echoes the request URL (which embeds the bot token)."""
    raise httpx.ConnectError(f"connect failed (request_url: '{request.url}')")


def _expect_redacted_transport_error(method_call):
    """Run ``method_call(api)`` against the URL-echoing transport and assert
    the raised BotAPIError message is token-free (the ``from exc`` cause
    chain keeps the full diagnostics for programmatic use)."""
    client = make_client(_url_echoing_handler)
    api = telegram_bot.BotAPI(BOT_TOKEN, client=client)

    async def go():
        try:
            with pytest.raises(telegram_bot.BotAPIError) as err:
                await method_call(api)
        finally:
            await client.aclose()
        assert BOT_TOKEN not in str(err.value)
        assert "[REDACTED]" in str(err.value)
        assert isinstance(err.value.__cause__, httpx.ConnectError)

    asyncio.run(go())


def test_redaction_call_transport_error():
    _expect_redacted_transport_error(lambda api: api.get_me())


def test_redaction_multipart_transport_error():
    _expect_redacted_transport_error(
        lambda api: api.send_document(7, "notes.md", b"hello", "text/markdown")
    )


def test_redaction_download_transport_error():
    _expect_redacted_transport_error(
        lambda api: api.download_file("cdn/path/FILE-1")
    )


def test_redaction_realistic_transport_error():
    """The resolved httpx phrases transport errors without the request URL
    (a bare reason string); the message must be token-free either way."""

    def handler(request):
        raise httpx.ConnectError("connection refused")

    client = make_client(handler)
    api = telegram_bot.BotAPI(BOT_TOKEN, client=client)

    async def go():
        try:
            with pytest.raises(telegram_bot.BotAPIError) as err:
                await api.get_me()
        finally:
            await client.aclose()
        assert BOT_TOKEN not in str(err.value)

    asyncio.run(go())


def test_main_invalid_token_prints_redacted_message(tmp_path, capsys):
    script = Script(
        [],
        fail_once_with=httpx.ConnectError(
            f"connect failed (request_url: 'https://api.telegram.org/bot{BOT_TOKEN}/getMe')"
        ),
    )
    client = make_client(script.handler)
    code = telegram_bot.main(BOT_TOKEN, tmp_path, client=client)
    assert code == 1
    err = capsys.readouterr().err
    assert "invalid Telegram bot token" in err
    assert BOT_TOKEN not in err
    assert "[REDACTED]" in err
    # getMe failed before touching disk
    assert not (tmp_path / "yask.db").exists()


def test_poll_loop_prints_redacted_message(capsys):
    script = run_bot_until_stop(
        Script(
            [[message_update(61, "/start")]],
            fail_once_with=httpx.ConnectError(
                f"connect failed (request_url: 'https://api.telegram.org/bot{BOT_TOKEN}/getUpdates')"
            ),
        )
    )
    err = capsys.readouterr().err
    assert "telegram poll failed" in err
    assert BOT_TOKEN not in err
    assert "[REDACTED]" in err
    # the run still recovers and answers /start
    assert len(script.sent) == 1
    assert script.sent[0]["text"] == telegram_bot.START_TEXT


def test_reply_for_dispatch_table():
    start = telegram_bot.reply_for("/start")
    assert isinstance(start, telegram_bot.KeyboardReply)
    assert start.text == telegram_bot.START_TEXT
    assert start.reply_markup == telegram_bot.menu_view().reply_markup
    help_reply = telegram_bot.reply_for("/help")
    assert isinstance(help_reply, telegram_bot.KeyboardReply)
    assert help_reply.text == telegram_bot.HELP_TEXT
    assert help_reply.reply_markup == {
        "inline_keyboard": [[telegram_bot._main_menu_button()]]
    }
    # the command token only: argument words after /start are ignored
    assert telegram_bot.reply_for("/start please") == start
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
    # the list is a rich surface: sendRichMessage with the keyboard, no
    # plain sendMessage leg
    assert len(script.sent_rich) == 1
    assert script.sent == []
    sent = script.sent_rich[0]
    assert sent["chat_id"] == 7
    # name order, id prefixes, zero states skipped, archived excluded
    assert sent["rich_message"]["markdown"] == (
        "# Projects\n"
        f"\n**{side}. side-project**\n"
        f"\n**{yask}. yask**\n"
        "- Backlog: 3\n"
        "- Todo: 2\n"
        "- In progress: 1\n"
        f"\n**{zeta}. zeta**\n"
        "- Blocked: 2"
    )
    # one button per project, in display order (the p: callback payloads),
    # then the Main-menu row (payload h)
    assert sent["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "side-project", "callback_data": f"p:{side}"}],
            [{"text": "yask", "callback_data": f"p:{yask}"}],
            [{"text": "zeta", "callback_data": f"p:{zeta}"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }
    # every payload is well under the Bot API's 64-byte callback_data limit
    for row in sent["reply_markup"]["inline_keyboard"]:
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


def test_projects_rich_fallback_to_html(store):
    """A failing rich leg degrades through the shared funnel: the HTML leg
    carries the markdown re-truncated to the regular budget, converted,
    with the per-project keyboard threaded — no double-send."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "working")
    store.move_task(pid, 1, "In progress", confirm=True)
    script = run_bot_until_stop(
        Script(
            [[message_update(99, "/projects")]],
            fail_rich_once=(400, "can't parse rich markdown"),
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # the rich leg fired (and failed) exactly once, full payload
    assert len(script.sent_rich) == 1
    expected = telegram_bot.project_view(store, 7)
    assert isinstance(expected, telegram_bot.RichReply)
    assert script.sent_rich[0]["rich_message"]["markdown"] == expected.markdown
    # the HTML leg replaced it — the markdown re-truncated to the regular
    # budget, converted, keyboard threaded
    assert len(script.sent) == 1
    body = script.sent[0]
    assert body["chat_id"] == 7
    assert body["parse_mode"] == "HTML"
    truncated = telegram_bot._truncate_inline(
        expected.markdown, len(expected.markdown), telegram_bot.REGULAR_TEXT_MAX
    )
    assert body["text"] == telegram_bot.markdown_to_html(truncated)
    assert body["reply_markup"] == expected.reply_markup


def test_projects_rich_kill_switch_off(store):
    """With rich disabled the /projects list takes the HTML leg first — no
    sendRichMessage call."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "backlog item")
    script = run_bot_until_stop(
        Script(
            [[message_update(100, "/projects")]],
            fail_rich_once=(400, "should never be called"),
        ),
        dispatch=telegram_bot.make_dispatch(store),
        rich=False,
    )
    assert script.sent_rich == []
    assert len(script.sent) == 1
    body = script.sent[0]
    assert body["chat_id"] == 7
    assert body["parse_mode"] == "HTML"
    expected = telegram_bot.project_view(store, 7)
    assert isinstance(expected, telegram_bot.RichReply)
    truncated = telegram_bot._truncate_inline(
        expected.markdown, len(expected.markdown), telegram_bot.REGULAR_TEXT_MAX
    )
    assert body["text"] == telegram_bot.markdown_to_html(truncated)
    assert body["reply_markup"] == expected.reply_markup


def test_projects_button_opens_tasks_view(store):
    """Pressing a /projects button must open the project's /tasks view."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    store.move_task(pid, t["number"], "In progress", confirm=True)
    # the button exactly as /projects emits it (the cross-task contract):
    # label = project name, payload "p:<pid>" — the keyboard rides the
    # rich reply
    first = run_bot_until_stop(
        Script([[message_update(93, "/projects")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    rows = first.sent_rich[0]["reply_markup"]["inline_keyboard"]
    assert rows == [
        [{"text": "yask", "callback_data": f"p:{pid}"}],
        [{"text": "Main menu", "callback_data": "h"}],
    ]
    payload = rows[0][0]["callback_data"]
    script = run_bot_until_stop(
        Script([[callback_update(94, payload, chat_id=11)]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the press is answered (no toast) and the project's /tasks view goes
    # out as a new message to the button's chat — the exact same view
    # typing "/tasks <id>" would send (rich markdown plus the t: keyboard)
    assert script.answered == [{"callback_query_id": "cbq-94"}]
    assert len(script.sent_rich) == 1
    assert script.sent == []
    expected = telegram_bot.tasks_view(store, str(pid), 11)
    assert isinstance(expected, telegram_bot.RichReply)
    assert script.sent_rich[0]["chat_id"] == 11
    assert script.sent_rich[0]["rich_message"]["markdown"] == expected.markdown
    assert script.sent_rich[0]["reply_markup"] == expected.reply_markup
    assert script.edited == []


def test_main_menu_button_from_projects_opens_menu(store):
    """Pressing the /projects board's Main-menu row opens the menu hub.

    The button is ``label "Main menu", payload "h"`` — the exact payload
    the /tasks, /task and notification keyboards emit too, and the h:
    family already pins (bare ``h`` → menu view as a new message), so one
    seeded-from-a-view press covers all of them.
    """
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "working")
    first = run_bot_until_stop(
        Script([[message_update(97, "/projects")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    rows = first.sent_rich[0]["reply_markup"]["inline_keyboard"]
    assert rows[-1] == [{"text": "Main menu", "callback_data": "h"}]
    payload = rows[-1][0]["callback_data"]
    script = run_bot_until_stop(
        Script([[callback_update(98, payload, chat_id=11)]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the press is answered (no toast) and the menu text+markup go out as
    # a new message to the button's chat; nothing is edited
    assert script.answered == [{"callback_query_id": "cbq-98"}]
    assert len(script.sent) == 1
    expected = telegram_bot.menu_view()
    assert script.sent[0]["chat_id"] == 11
    assert script.sent[0]["text"] == expected.text
    assert script.sent[0]["reply_markup"] == expected.reply_markup
    assert script.edited == []


def test_main_menu_button_from_help_opens_menu(store):
    """Pressing the /help reply's Main-menu row opens the menu hub.

    The button is seeded from the real /help reply markup (the same
    ``label "Main menu", payload "h"`` row the board views emit), so the
    help → hub link is pinned end to end (#62).
    """
    first = run_bot_until_stop(
        Script([[message_update(413, "/help")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    rows = first.sent[0]["reply_markup"]["inline_keyboard"]
    assert rows == [[{"text": "Main menu", "callback_data": "h"}]]
    payload = rows[0][0]["callback_data"]
    script = run_bot_until_stop(
        Script([[callback_update(414, payload, chat_id=11)]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the press is answered (no toast) and the hub's text+markup go out
    # as a new message to the button's chat; nothing is edited
    assert script.answered == [{"callback_query_id": "cbq-414"}]
    assert len(script.sent) == 1
    expected = telegram_bot.menu_view()
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
    payload = first.sent_rich[0]["reply_markup"]["inline_keyboard"][0][0][
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


# --- main-menu hub (menu_view + the h: callback family) ----------------------


def test_menu_view_layout():
    """The exact menu text and keyboard the tests pin down."""
    view = telegram_bot.menu_view()
    assert view.text == "Main menu — tap a button to open a board view."
    assert view.reply_markup == {
        "inline_keyboard": [
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
    }
    # every payload stays well under Telegram's 64-byte callback_data cap
    for row in view.reply_markup["inline_keyboard"]:
        for button in row:
            assert len(button["callback_data"].encode()) < 64


def test_menu_home_renders_menu(store):
    """Pressing the bare h (home) payload sends the menu view, no toast."""
    script = run_bot_until_stop(
        Script([[callback_update(400, "h", chat_id=11)]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the press is answered (no toast) and the menu's text+markup go out
    # as a new message to the button's chat; nothing is edited
    assert script.answered == [{"callback_query_id": "cbq-400"}]
    assert len(script.sent) == 1
    expected = telegram_bot.menu_view()
    assert script.sent[0]["chat_id"] == 11
    assert script.sent[0]["text"] == expected.text
    assert script.sent[0]["reply_markup"] == expected.reply_markup
    assert script.edited == []


def test_menu_button_projects_opens_projects_view(store):
    """h:p opens the same view /projects sends — rich for the pressing
    chat (the callback's chat id)."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "backlog item")
    script = run_bot_until_stop(
        Script([[callback_update(401, "h:p", chat_id=11)]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert script.answered == [{"callback_query_id": "cbq-401"}]
    assert len(script.sent_rich) == 1
    assert script.sent == []
    expected = telegram_bot.project_view(store, 11)
    assert isinstance(expected, telegram_bot.RichReply)
    assert script.sent_rich[0]["chat_id"] == 11
    assert script.sent_rich[0]["rich_message"]["markdown"] == expected.markdown
    assert script.sent_rich[0]["reply_markup"] == expected.reply_markup
    assert script.edited == []


def test_menu_button_projects_inaccessible_message_keeps_keyboard(store):
    """h:p on an inaccessible original message (no message key → no chat)
    answers the plain KeyboardReply form *with* the per-project keyboard —
    the tap targets must survive the missing chat (the deliberate
    divergence from format_task_view's keyboard-less plain form)."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "backlog item")
    update = callback_update(407, "h:p", chat_id=11)
    del update["callback_query"]["message"]
    dispatch = telegram_bot.make_callback_dispatch(store)
    action = dispatch(update["callback_query"])
    assert action is not None
    assert action.answer_text is None
    assert action.edit is None
    reply = action.reply
    assert isinstance(reply, telegram_bot.KeyboardReply)
    assert reply == telegram_bot.project_view(store)
    assert reply.text == f"Projects:\n{pid}. yask — Backlog: 1"
    assert reply.reply_markup == {
        "inline_keyboard": [
            [{"text": "yask", "callback_data": f"p:{pid}"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }


def test_menu_button_tasks_opens_tasks_view(store):
    """h:t opens the same all-projects view /tasks sends — rich for the
    pressing chat (the callback's chat id)."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    store.move_task(pid, t["number"], "In progress", confirm=True)
    script = run_bot_until_stop(
        Script([[callback_update(402, "h:t", chat_id=11)]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert script.answered == [{"callback_query_id": "cbq-402"}]
    assert len(script.sent_rich) == 1
    assert script.sent == []
    expected = telegram_bot.tasks_view(store, chat_id=11)
    assert isinstance(expected, telegram_bot.RichReply)
    assert script.sent_rich[0]["chat_id"] == 11
    assert script.sent_rich[0]["rich_message"]["markdown"] == expected.markdown
    assert script.sent_rich[0]["reply_markup"] == expected.reply_markup
    assert script.edited == []


def test_menu_button_tasks_inaccessible_message_keeps_keyboard(store):
    """h:t and p: presses on an inaccessible original message (no message
    key → no chat) answer the plain KeyboardReply form *with* the per-task
    keyboard — the tap targets must survive the missing chat (the
    deliberate divergence from format_task_view's keyboard-less plain
    form)."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    store.move_task(pid, t["number"], "In progress", confirm=True)
    dispatch = telegram_bot.make_callback_dispatch(store)

    # the menu's Tasks route (h:t): the all-projects view
    update = callback_update(416, "h:t", chat_id=11)
    del update["callback_query"]["message"]
    action = dispatch(update["callback_query"])
    assert action is not None
    assert action.answer_text is None
    assert action.edit is None
    reply = action.reply
    assert isinstance(reply, telegram_bot.KeyboardReply)
    assert reply == telegram_bot.tasks_view(store)
    assert reply.text == (
        f"Tasks in progress:\n"
        f"{pid}. yask\n"
        "  In progress:\n"
        f"    #{t['number']} working"
    )
    assert reply.reply_markup == {
        "inline_keyboard": [
            [
                {
                    "text": f"#{t['number']} working",
                    "callback_data": f"t:{pid}:{t['number']}",
                }
            ],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }

    # the /projects drill-down (p:<pid>): the same view resolved by id
    update = callback_update(417, f"p:{pid}", chat_id=11)
    del update["callback_query"]["message"]
    action = dispatch(update["callback_query"])
    assert action is not None
    assert action.answer_text is None
    assert action.edit is None
    reply = action.reply
    assert isinstance(reply, telegram_bot.KeyboardReply)
    assert reply == telegram_bot.tasks_view(store, str(pid))


def test_menu_button_subscriptions(store):
    """h:s lists the button's chat's subscriptions (the /subscribe view)."""
    script = run_bot_until_stop(
        Script([[callback_update(403, "h:s")]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == "Your subscriptions:\n(none)"

    zeta = store.create_project("zeta")["id"]
    alpha = store.create_project("alpha")["id"]
    store.subscribe_project(7, zeta)
    store.subscribe_project(7, alpha)
    script = run_bot_until_stop(
        Script([[callback_update(404, "h:s")]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # same shape as /subscribe: id-prefixed lines in name order, no keyboard
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == (
        "Your subscriptions:\n"
        f"{alpha}. alpha\n"
        f"{zeta}. zeta"
    )
    assert "reply_markup" not in script.sent[0]
    assert script.edited == []


def test_menu_button_add_task(store):
    """h:a answers with the /add usage (the board's task types listed)."""
    script = run_bot_until_stop(
        Script([[callback_update(405, "h:a", chat_id=11)]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert script.answered == [{"callback_query_id": "cbq-405"}]
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 11
    assert script.sent[0]["text"] == telegram_bot.add_usage_text(store)
    assert "reply_markup" not in script.sent[0]
    assert script.edited == []


def test_menu_button_add_task_store_failure(store, monkeypatch):
    """h:a reads the board's task types; a store failure replies with the
    read-family error text."""

    def boom():
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "list_task_types", boom)
    script = run_bot_until_stop(
        Script([[callback_update(412, "h:a", chat_id=11)]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert script.answered == [{"callback_query_id": "cbq-412"}]
    assert len(script.sent) == 1
    assert script.sent[0]["text"] == telegram_bot.TASKS_ERROR_TEXT


def test_menu_button_help(store):
    """h:h answers with the static help text."""
    script = run_bot_until_stop(
        Script([[callback_update(406, "h:h", chat_id=11)]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert script.answered == [{"callback_query_id": "cbq-406"}]
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 11
    assert script.sent[0]["text"] == telegram_bot.HELP_TEXT
    assert "reply_markup" not in script.sent[0]
    assert script.edited == []


def test_menu_store_failure_replies_and_recovers(store, monkeypatch):
    """A store failure on an h: board route replies with the error text."""
    store.create_project("yask")

    def boom_overviews():
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "list_project_overviews", boom_overviews)
    script = run_bot_until_stop(
        Script(
            [
                [callback_update(407, "h:p", chat_id=11), message_update(408, "/start")]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the press is answered (no toast), the failure produces an error reply,
    # and the loop survives: the follow-up message is still answered
    assert script.answered == [{"callback_query_id": "cbq-407"}]
    assert len(script.sent) == 2
    assert script.sent[0]["chat_id"] == 11
    assert script.sent[0]["text"] == telegram_bot.PROJECTS_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT
    assert script.offsets == [None, 409]

    def boom_subs(chat_id):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "list_subscriptions", boom_subs)
    script = run_bot_until_stop(
        Script(
            [
                [callback_update(409, "h:s", chat_id=11), message_update(410, "/start")]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert script.answered == [{"callback_query_id": "cbq-409"}]
    assert len(script.sent) == 2
    assert script.sent[0]["chat_id"] == 11
    assert script.sent[0]["text"] == telegram_bot.SUBSCRIBE_ERROR_TEXT
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
    # the list is a rich surface: sendRichMessage with the keyboard, no
    # plain sendMessage leg
    assert len(script.sent_rich) == 1
    assert script.sent == []
    sent = script.sent_rich[0]
    assert sent["chat_id"] == 7
    # exact markdown: name order, state grouping/order, id prefixes, and
    # the excluded states absent
    assert sent["rich_message"]["markdown"] == (
        "# Tasks in progress\n"
        f"\n**{alpha}. alpha**\n"
        "**Todo:**\n"
        "- #2 todo 1\n"
        "**Planning:**\n"
        "- #3 planning 1\n"
        "**In progress:**\n"
        "- #4 working\n"
        "**Review:**\n"
        "- #5 review 1\n"
        f"\n**{zeta}. zeta**\n"
        "**In progress:**\n"
        "- #1 z working"
    )
    # one button per task, in reading order (the t: callback payloads),
    # then the Main-menu row (payload h)
    assert sent["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#2 todo 1", "callback_data": f"t:{alpha}:2"}],
            [{"text": "#3 planning 1", "callback_data": f"t:{alpha}:3"}],
            [{"text": "#4 working", "callback_data": f"t:{alpha}:4"}],
            [{"text": "#5 review 1", "callback_data": f"t:{alpha}:5"}],
            [{"text": "#1 z working", "callback_data": f"t:{zeta}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }
    # every payload is well under the Bot API's 64-byte callback_data limit
    for row in sent["reply_markup"]["inline_keyboard"]:
        assert len(row[0]["callback_data"].encode("utf-8")) < 64


def test_tasks_filter_by_id_and_name(store):
    zeta = store.create_project("zeta")["id"]
    alpha = store.create_project("alpha")["id"]
    t = store.create_task(alpha, "alpha working")
    store.move_task(alpha, t["number"], "In progress", confirm=True)
    t = store.create_task(zeta, "zeta working")
    store.move_task(zeta, t["number"], "In progress", confirm=True)

    # by project name, case-insensitive — the list is a rich surface
    script = run_bot_until_stop(
        Script([[message_update(111, "/tasks ALPHA")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent_rich) == 1
    assert script.sent == []
    assert script.sent_rich[0]["rich_message"]["markdown"] == (
        "# Tasks in progress\n"
        f"\n**{alpha}. alpha**\n"
        "**In progress:**\n"
        "- #1 alpha working"
    )
    assert script.sent_rich[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 alpha working", "callback_data": f"t:{alpha}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }

    # by project id
    script = run_bot_until_stop(
        Script([[message_update(112, f"/tasks {zeta}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent_rich) == 1
    assert script.sent == []
    assert script.sent_rich[0]["rich_message"]["markdown"] == (
        "# Tasks in progress\n"
        f"\n**{zeta}. zeta**\n"
        "**In progress:**\n"
        "- #1 zeta working"
    )
    assert script.sent_rich[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 zeta working", "callback_data": f"t:{zeta}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
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
    assert len(script.sent_rich) == 1
    assert script.sent == []
    assert script.sent_rich[0]["rich_message"]["markdown"] == (
        "# Tasks in progress\n"
        f"\n**{pid}. my big project**\n"
        "**In progress:**\n"
        "- #1 working"
    )
    assert script.sent_rich[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 working", "callback_data": f"t:{pid}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
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


def test_tasks_rich_fallback_to_html(store):
    """A failing rich leg degrades through the shared funnel: the HTML leg
    carries the markdown re-truncated to the regular budget, converted,
    with the per-task keyboard threaded — no double-send."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "working")
    store.move_task(pid, 1, "In progress", confirm=True)
    script = run_bot_until_stop(
        Script(
            [[message_update(165, "/tasks")]],
            fail_rich_once=(400, "can't parse rich markdown"),
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # the rich leg fired (and failed) exactly once, full payload
    assert len(script.sent_rich) == 1
    expected = telegram_bot.tasks_view(store, chat_id=7)
    assert isinstance(expected, telegram_bot.RichReply)
    assert script.sent_rich[0]["rich_message"]["markdown"] == expected.markdown
    # the HTML leg replaced it — the markdown re-truncated to the regular
    # budget, converted, keyboard threaded
    assert len(script.sent) == 1
    body = script.sent[0]
    assert body["chat_id"] == 7
    assert body["parse_mode"] == "HTML"
    truncated = telegram_bot._truncate_inline(
        expected.markdown, len(expected.markdown), telegram_bot.REGULAR_TEXT_MAX
    )
    assert body["text"] == telegram_bot.markdown_to_html(truncated)
    assert body["reply_markup"] == expected.reply_markup


def test_tasks_rich_kill_switch_off(store):
    """With rich disabled the /tasks list takes the HTML leg first — no
    sendRichMessage call."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "working")
    store.move_task(pid, 1, "In progress", confirm=True)
    script = run_bot_until_stop(
        Script(
            [[message_update(166, "/tasks")]],
            fail_rich_once=(400, "should never be called"),
        ),
        dispatch=telegram_bot.make_dispatch(store),
        rich=False,
    )
    assert script.sent_rich == []
    assert len(script.sent) == 1
    body = script.sent[0]
    assert body["chat_id"] == 7
    assert body["parse_mode"] == "HTML"
    expected = telegram_bot.tasks_view(store, chat_id=7)
    assert isinstance(expected, telegram_bot.RichReply)
    truncated = telegram_bot._truncate_inline(
        expected.markdown, len(expected.markdown), telegram_bot.REGULAR_TEXT_MAX
    )
    assert body["text"] == telegram_bot.markdown_to_html(truncated)
    assert body["reply_markup"] == expected.reply_markup


def test_tasks_overflow_degrades(store):
    """A board whose markdown exceeds the rich budget 400s the rich leg and
    degrades through the shared funnel: the HTML leg sends the converted,
    re-truncated markdown with the keyboard (no new cap is added)."""
    pid = store.create_project("yask")["id"]
    # ~73 chars per task line (the markdown form): 520 tasks comfortably
    # exceed RICH_MESSAGE_MAX (32 768 chars).
    for i in range(520):
        t = store.create_task(
            pid, f"overflow task number {i:04d} " + "x" * 40
        )
        store.move_task(pid, t["number"], "In progress", confirm=True)
    expected = telegram_bot.tasks_view(store, chat_id=7)
    assert isinstance(expected, telegram_bot.RichReply)
    assert len(expected.markdown) > telegram_bot.RICH_MESSAGE_MAX
    script = run_bot_until_stop(
        Script(
            [[message_update(167, "/tasks")]],
            fail_rich_once=(400, "too long"),
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # the rich leg fired (and failed) exactly once, full payload
    assert len(script.sent_rich) == 1
    assert script.sent_rich[0]["rich_message"]["markdown"] == expected.markdown
    # the HTML leg replaced it — the markdown re-truncated to the regular
    # budget, converted, keyboard threaded
    assert len(script.sent) == 1
    body = script.sent[0]
    assert body["chat_id"] == 7
    assert body["parse_mode"] == "HTML"
    truncated = telegram_bot._truncate_inline(
        expected.markdown, len(expected.markdown), telegram_bot.REGULAR_TEXT_MAX
    )
    assert body["text"] == telegram_bot.markdown_to_html(truncated)
    assert body["reply_markup"] == expected.reply_markup


def test_help_mentions_tasks():
    assert "/tasks" in telegram_bot.HELP_TEXT


# --- /backlog (store-backed dispatch) ----------------------------------------


def test_backlog_empty_board(store):
    script = run_bot_until_stop(
        Script([[message_update(181, "/backlog")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == "Backlog:\n(none)"
    # no tasks → no keyboard (the Bot API rejects an empty inline_keyboard)
    assert "reply_markup" not in script.sent[0]


def test_backlog_populated_board_exact(store):
    # zeta is created first (id 1) but "alpha" sorts before "zeta", so the
    # id-2 project is listed first — display order is by name.
    zeta = store.create_project("zeta")["id"]
    alpha = store.create_project("alpha")["id"]

    # alpha: two Backlog tasks plus one task in each other state
    store.create_task(alpha, "backlog 1")                        # #1 Backlog
    store.create_task(alpha, "backlog 2")                        # #2 Backlog
    t = store.create_task(alpha, "todo")
    store.move_task(alpha, t["number"], "Todo", confirm=True)    # #3
    t = store.create_task(alpha, "working")
    store.move_task(alpha, t["number"], "In progress", confirm=True)  # #4
    t = store.create_task(alpha, "done")
    store.move_task(alpha, t["number"], "Done", confirm=True)    # #5
    t = store.create_task(alpha, "blocked")
    store.move_task(alpha, t["number"], "Blocked")               # #6
    t = store.create_task(alpha, "archived")
    store.archive_task(alpha, t["number"], confirm=True)         # #7

    # zeta: a single Backlog task
    store.create_task(zeta, "z backlog")                         # #1

    script = run_bot_until_stop(
        Script([[message_update(182, "/backlog")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # the list is a rich surface: sendRichMessage with the keyboard, no
    # plain sendMessage leg
    assert len(script.sent_rich) == 1
    assert script.sent == []
    sent = script.sent_rich[0]
    assert sent["chat_id"] == 7
    # exact markdown: name order, id prefixes, column order, and the
    # non-Backlog states absent
    assert sent["rich_message"]["markdown"] == (
        "# Backlog\n"
        f"\n**{alpha}. alpha**\n"
        "- #1 backlog 1\n"
        "- #2 backlog 2\n"
        f"\n**{zeta}. zeta**\n"
        "- #1 z backlog"
    )
    # one button per task, in reading order (the t: callback payloads),
    # then the Main-menu row (payload h)
    assert sent["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 backlog 1", "callback_data": f"t:{alpha}:1"}],
            [{"text": "#2 backlog 2", "callback_data": f"t:{alpha}:2"}],
            [{"text": "#1 z backlog", "callback_data": f"t:{zeta}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }
    # every payload is well under the Bot API's 64-byte callback_data limit
    for row in sent["reply_markup"]["inline_keyboard"]:
        assert len(row[0]["callback_data"].encode("utf-8")) < 64


def test_backlog_filter_by_id_and_name(store):
    zeta = store.create_project("zeta")["id"]
    alpha = store.create_project("alpha")["id"]
    store.create_task(alpha, "alpha backlog")
    t = store.create_task(alpha, "alpha working")
    store.move_task(alpha, t["number"], "In progress", confirm=True)
    store.create_task(zeta, "zeta backlog")

    # by project name, case-insensitive — the list is a rich surface
    script = run_bot_until_stop(
        Script([[message_update(183, "/backlog ALPHA")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent_rich) == 1
    assert script.sent == []
    assert script.sent_rich[0]["rich_message"]["markdown"] == (
        "# Backlog\n"
        f"\n**{alpha}. alpha**\n"
        "- #1 alpha backlog"
    )
    assert script.sent_rich[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 alpha backlog", "callback_data": f"t:{alpha}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }

    # by project id
    script = run_bot_until_stop(
        Script([[message_update(184, f"/backlog {zeta}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent_rich) == 1
    assert script.sent == []
    assert script.sent_rich[0]["rich_message"]["markdown"] == (
        "# Backlog\n"
        f"\n**{zeta}. zeta**\n"
        "- #1 zeta backlog"
    )
    assert script.sent_rich[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 zeta backlog", "callback_data": f"t:{zeta}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }


def test_backlog_filter_name_with_spaces(store):
    pid = store.create_project("my big project")["id"]
    store.create_task(pid, "backlog")
    script = run_bot_until_stop(
        Script([[message_update(185, "/backlog my big project")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent_rich) == 1
    assert script.sent == []
    assert script.sent_rich[0]["rich_message"]["markdown"] == (
        "# Backlog\n"
        f"\n**{pid}. my big project**\n"
        "- #1 backlog"
    )
    assert script.sent_rich[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 backlog", "callback_data": f"t:{pid}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }


def test_backlog_filter_zero_backlog(store):
    alpha = store.create_project("alpha")["id"]
    zeta = store.create_project("zeta")["id"]
    # alpha has only active-state tasks (no Backlog)
    t = store.create_task(alpha, "working only")
    store.move_task(alpha, t["number"], "In progress", confirm=True)
    # zeta has a Backlog task, so the board is non-empty overall
    store.create_task(zeta, "z backlog")

    # project found but zero Backlog tasks
    script = run_bot_until_stop(
        Script([[message_update(186, "/backlog alpha")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == "Backlog:\n(none)"
    # no tasks → no keyboard (the Bot API rejects an empty inline_keyboard)
    assert "reply_markup" not in script.sent[0]


def test_backlog_unknown_project(store):
    store.create_project("alpha")
    script = run_bot_until_stop(
        Script([[message_update(187, "/backlog nope")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project 'nope' not found. Use /projects to list projects."
    )
    assert "reply_markup" not in script.sent[0]
    # unknown by id
    script = run_bot_until_stop(
        Script([[message_update(188, "/backlog 999")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project '999' not found. Use /projects to list projects."
    )
    assert "reply_markup" not in script.sent[0]


def test_backlog_with_bot_mention(store):
    script = run_bot_until_stop(
        Script([[message_update(189, "/backlog@yask_test_bot")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == "Backlog:\n(none)"
    assert "reply_markup" not in script.sent[0]


def test_backlog_store_failure_replies_and_recovers(store, monkeypatch):
    # a project must exist so the no-arg view reaches list_backlog
    store.create_project("alpha")

    def boom(project_id):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "list_backlog", boom)
    script = run_bot_until_stop(
        Script([[message_update(190, "/backlog"), message_update(191, "/start")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # the failure produces a reply, not a crash; the next message is still
    # answered
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == telegram_bot.BACKLOG_ERROR_TEXT
    assert "reply_markup" not in script.sent[0]
    assert script.sent[1]["text"] == telegram_bot.START_TEXT


def test_backlog_rich_fallback_to_html(store):
    """A failing rich leg degrades through the shared funnel: the HTML leg
    carries the markdown re-truncated to the regular budget, converted,
    with the per-task keyboard threaded — no double-send."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "backlog item")
    script = run_bot_until_stop(
        Script(
            [[message_update(193, "/backlog")]],
            fail_rich_once=(400, "can't parse rich markdown"),
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # the rich leg fired (and failed) exactly once, full payload
    assert len(script.sent_rich) == 1
    expected = telegram_bot.backlog_view(store, chat_id=7)
    assert isinstance(expected, telegram_bot.RichReply)
    assert script.sent_rich[0]["rich_message"]["markdown"] == expected.markdown
    # the HTML leg replaced it — the markdown re-truncated to the regular
    # budget, converted, keyboard threaded
    assert len(script.sent) == 1
    body = script.sent[0]
    assert body["chat_id"] == 7
    assert body["parse_mode"] == "HTML"
    truncated = telegram_bot._truncate_inline(
        expected.markdown, len(expected.markdown), telegram_bot.REGULAR_TEXT_MAX
    )
    assert body["text"] == telegram_bot.markdown_to_html(truncated)
    assert body["reply_markup"] == expected.reply_markup


def test_backlog_rich_kill_switch_off(store):
    """With rich disabled the /backlog list takes the HTML leg first — no
    sendRichMessage call."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "backlog item")
    script = run_bot_until_stop(
        Script(
            [[message_update(194, "/backlog")]],
            fail_rich_once=(400, "should never be called"),
        ),
        dispatch=telegram_bot.make_dispatch(store),
        rich=False,
    )
    assert script.sent_rich == []
    assert len(script.sent) == 1
    body = script.sent[0]
    assert body["chat_id"] == 7
    assert body["parse_mode"] == "HTML"
    expected = telegram_bot.backlog_view(store, chat_id=7)
    assert isinstance(expected, telegram_bot.RichReply)
    truncated = telegram_bot._truncate_inline(
        expected.markdown, len(expected.markdown), telegram_bot.REGULAR_TEXT_MAX
    )
    assert body["text"] == telegram_bot.markdown_to_html(truncated)
    assert body["reply_markup"] == expected.reply_markup


def test_help_mentions_backlog():
    assert "/backlog" in telegram_bot.HELP_TEXT


# --- /blocked (store-backed dispatch) ----------------------------------------


def test_blocked_empty_board(store):
    script = run_bot_until_stop(
        Script([[message_update(920, "/blocked")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == "Blocked:\n(none)"
    # no tasks → no keyboard (the Bot API rejects an empty inline_keyboard)
    assert "reply_markup" not in script.sent[0]


def test_blocked_populated_board_exact(store):
    # zeta is created first (id 1) but "alpha" sorts before "zeta", so the
    # id-2 project is listed first — display order is by name.
    zeta = store.create_project("zeta")["id"]
    alpha = store.create_project("alpha")["id"]

    # alpha: two Blocked tasks plus one task in each other state
    t = store.create_task(alpha, "blocked 1")
    store.move_task(alpha, t["number"], "Blocked")               # #1
    t = store.create_task(alpha, "blocked 2")
    store.move_task(alpha, t["number"], "Blocked")               # #2
    store.create_task(alpha, "backlog")                        # #3 Backlog
    t = store.create_task(alpha, "todo")
    store.move_task(alpha, t["number"], "Todo", confirm=True)    # #4
    t = store.create_task(alpha, "working")
    store.move_task(alpha, t["number"], "In progress", confirm=True)  # #5
    t = store.create_task(alpha, "done")
    store.move_task(alpha, t["number"], "Done", confirm=True)    # #6
    t = store.create_task(alpha, "archived")
    store.archive_task(alpha, t["number"], confirm=True)         # #7

    # zeta: a single Blocked task
    t = store.create_task(zeta, "z blocked")                    # #1
    store.move_task(zeta, t["number"], "Blocked")

    script = run_bot_until_stop(
        Script([[message_update(921, "/blocked")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # the list is a rich surface: sendRichMessage with the keyboard, no
    # plain sendMessage leg
    assert len(script.sent_rich) == 1
    assert script.sent == []
    sent = script.sent_rich[0]
    assert sent["chat_id"] == 7
    # exact markdown: name order, id prefixes, column order, and the
    # non-Blocked states absent
    assert sent["rich_message"]["markdown"] == (
        "# Blocked\n"
        f"\n**{alpha}. alpha**\n"
        "- #1 blocked 1\n"
        "- #2 blocked 2\n"
        f"\n**{zeta}. zeta**\n"
        "- #1 z blocked"
    )
    # one button per task, in reading order (the t: callback payloads),
    # then the Main-menu row (payload h)
    assert sent["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 blocked 1", "callback_data": f"t:{alpha}:1"}],
            [{"text": "#2 blocked 2", "callback_data": f"t:{alpha}:2"}],
            [{"text": "#1 z blocked", "callback_data": f"t:{zeta}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }
    # every payload is well under the Bot API's 64-byte callback_data limit
    for row in sent["reply_markup"]["inline_keyboard"]:
        assert len(row[0]["callback_data"].encode("utf-8")) < 64


def test_blocked_filter_by_id_and_name(store):
    zeta = store.create_project("zeta")["id"]
    alpha = store.create_project("alpha")["id"]
    t = store.create_task(alpha, "alpha blocked")
    store.move_task(alpha, t["number"], "Blocked")
    t = store.create_task(alpha, "alpha working")
    store.move_task(alpha, t["number"], "In progress", confirm=True)
    t = store.create_task(zeta, "zeta blocked")
    store.move_task(zeta, t["number"], "Blocked")

    # by project name, case-insensitive — the list is a rich surface
    script = run_bot_until_stop(
        Script([[message_update(922, "/blocked ALPHA")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent_rich) == 1
    assert script.sent == []
    assert script.sent_rich[0]["rich_message"]["markdown"] == (
        "# Blocked\n"
        f"\n**{alpha}. alpha**\n"
        "- #1 alpha blocked"
    )
    assert script.sent_rich[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 alpha blocked", "callback_data": f"t:{alpha}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }

    # by project id
    script = run_bot_until_stop(
        Script([[message_update(923, f"/blocked {zeta}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent_rich) == 1
    assert script.sent == []
    assert script.sent_rich[0]["rich_message"]["markdown"] == (
        "# Blocked\n"
        f"\n**{zeta}. zeta**\n"
        "- #1 zeta blocked"
    )
    assert script.sent_rich[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 zeta blocked", "callback_data": f"t:{zeta}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }


def test_blocked_filter_name_with_spaces(store):
    pid = store.create_project("my big project")["id"]
    t = store.create_task(pid, "blocked")
    store.move_task(pid, t["number"], "Blocked")
    script = run_bot_until_stop(
        Script([[message_update(924, "/blocked my big project")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent_rich) == 1
    assert script.sent == []
    assert script.sent_rich[0]["rich_message"]["markdown"] == (
        "# Blocked\n"
        f"\n**{pid}. my big project**\n"
        "- #1 blocked"
    )
    assert script.sent_rich[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 blocked", "callback_data": f"t:{pid}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }


def test_blocked_filter_zero_blocked(store):
    alpha = store.create_project("alpha")["id"]
    zeta = store.create_project("zeta")["id"]
    # alpha has only active-state tasks (no Blocked)
    t = store.create_task(alpha, "working only")
    store.move_task(alpha, t["number"], "In progress", confirm=True)
    # zeta has a Blocked task, so the board is non-empty overall
    t = store.create_task(zeta, "z blocked")
    store.move_task(zeta, t["number"], "Blocked")

    # project found but zero Blocked tasks
    script = run_bot_until_stop(
        Script([[message_update(925, "/blocked alpha")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == "Blocked:\n(none)"
    # no tasks → no keyboard (the Bot API rejects an empty inline_keyboard)
    assert "reply_markup" not in script.sent[0]


def test_blocked_unknown_project(store):
    store.create_project("alpha")
    script = run_bot_until_stop(
        Script([[message_update(926, "/blocked nope")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project 'nope' not found. Use /projects to list projects."
    )
    assert "reply_markup" not in script.sent[0]
    # unknown by id
    script = run_bot_until_stop(
        Script([[message_update(927, "/blocked 999")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project '999' not found. Use /projects to list projects."
    )
    assert "reply_markup" not in script.sent[0]


def test_blocked_with_bot_mention(store):
    script = run_bot_until_stop(
        Script([[message_update(928, "/blocked@yask_test_bot")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == "Blocked:\n(none)"
    assert "reply_markup" not in script.sent[0]


def test_blocked_store_failure_replies_and_recovers(store, monkeypatch):
    # a project must exist so the no-arg view reaches list_blocked
    store.create_project("alpha")

    def boom(project_id):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "list_blocked", boom)
    script = run_bot_until_stop(
        Script([[message_update(929, "/blocked"), message_update(930, "/start")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # the failure produces a reply, not a crash; the next message is still
    # answered
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == telegram_bot.BLOCKED_ERROR_TEXT
    assert "reply_markup" not in script.sent[0]
    assert script.sent[1]["text"] == telegram_bot.START_TEXT


def test_blocked_rich_fallback_to_html(store):
    """A failing rich leg degrades through the shared funnel: the HTML leg
    carries the markdown re-truncated to the regular budget, converted,
    with the per-task keyboard threaded — no double-send."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "blocked item")
    store.move_task(pid, t["number"], "Blocked")
    script = run_bot_until_stop(
        Script(
            [[message_update(931, "/blocked")]],
            fail_rich_once=(400, "can't parse rich markdown"),
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # the rich leg fired (and failed) exactly once, full payload
    assert len(script.sent_rich) == 1
    expected = telegram_bot.blocked_view(store, chat_id=7)
    assert isinstance(expected, telegram_bot.RichReply)
    assert script.sent_rich[0]["rich_message"]["markdown"] == expected.markdown
    # the HTML leg replaced it — the markdown re-truncated to the regular
    # budget, converted, keyboard threaded
    assert len(script.sent) == 1
    body = script.sent[0]
    assert body["chat_id"] == 7
    assert body["parse_mode"] == "HTML"
    truncated = telegram_bot._truncate_inline(
        expected.markdown, len(expected.markdown), telegram_bot.REGULAR_TEXT_MAX
    )
    assert body["text"] == telegram_bot.markdown_to_html(truncated)
    assert body["reply_markup"] == expected.reply_markup


def test_blocked_rich_kill_switch_off(store):
    """With rich disabled the /blocked list takes the HTML leg first — no
    sendRichMessage call."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "blocked item")
    store.move_task(pid, t["number"], "Blocked")
    script = run_bot_until_stop(
        Script(
            [[message_update(932, "/blocked")]],
            fail_rich_once=(400, "should never be called"),
        ),
        dispatch=telegram_bot.make_dispatch(store),
        rich=False,
    )
    assert script.sent_rich == []
    assert len(script.sent) == 1
    body = script.sent[0]
    assert body["chat_id"] == 7
    assert body["parse_mode"] == "HTML"
    expected = telegram_bot.blocked_view(store, chat_id=7)
    assert isinstance(expected, telegram_bot.RichReply)
    truncated = telegram_bot._truncate_inline(
        expected.markdown, len(expected.markdown), telegram_bot.REGULAR_TEXT_MAX
    )
    assert body["text"] == telegram_bot.markdown_to_html(truncated)
    assert body["reply_markup"] == expected.reply_markup


def test_help_mentions_blocked():
    assert "/blocked" in telegram_bot.HELP_TEXT


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


def expected_inline_text(number, title, filename, body):
    """The inline attachment message the bot sends for a small text
    attachment: the ``#<n> <title> — <filename>`` header line plus the
    decoded body, truncated to ``INLINE_TEXT_MAX`` with a
    ``… (truncated, N chars total)`` note when it does not fit.
    """
    text = f"#{number} {title} — {filename}\n{body}"
    if len(text) > telegram_bot.INLINE_TEXT_MAX:
        note = f"\n… (truncated, {len(body)} chars total)"
        budget = max(0, telegram_bot.INLINE_TEXT_MAX - len(note))
        text = text[:budget] + note
    return text


def expected_rich_markdown(number, title, filename, body):
    """The Rich Message markdown the bot sends for a small markdown
    attachment: the ``#<n> <title> — <filename>`` context line plus the
    raw body (no truncation — the 16 KiB inline threshold fits the Rich
    Message budget; the fallback legs re-truncate at send time).
    """
    return f"#{number} {title} — {filename}\n{body}"


def expected_task_text(store, d):
    """The exact /task detail markdown for the seed_task_view task.

    Blocks joined by one blank line: the header (H1 heading, then the
    State/Estimate/Parent bold lines), the raw description, and the
    Prerequisites/Attachments/History list blocks.
    """
    pid, t = d["pid"], d["t"]
    history = store.get_history(pid, t["number"])
    assert len(history) == 3
    return (
        f"# {t['number']} working — Story\n"
        "**State:** In progress\n"
        "**Estimate:** 3\n"
        f"**Parent:** #{d['epic']['number']}\n"
        "\n"
        "The task at hand.\n"
        "\n"
        "**Prerequisites:**\n"
        f"- #{d['p1']['number']} prereq one — Review\n"
        f"- #{d['p2']['number']} prereq two — In progress\n"
        "\n"
        "**Attachments:**\n"
        f"- {d['plan']['id']}. plan.md (7.6 KB) — /attachment yask "
        f"{t['number']} {d['plan']['id']}\n"
        f"- {d['img']['id']}. img.png (108 B) — /attachment yask "
        f"{t['number']} {d['img']['id']}\n"
        "\n"
        "**History:**\n"
        f"- {history[0]['changed_at']} — created (web)\n"
        f"- {history[1]['changed_at']} — Backlog → Todo (web)\n"
        f"- {history[2]['changed_at']} — Todo → In progress (web)"
    )


def test_task_by_number_exact(store):
    d = seed_task_view(store)
    script = run_bot_until_stop(
        Script([[message_update(231, f"/task yask {d['t']['number']}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # the detail goes out as a Rich Message, not sendMessage
    assert len(script.sent_rich) == 1
    assert script.sent_rich[0]["chat_id"] == 7
    assert (
        script.sent_rich[0]["rich_message"]["markdown"]
        == expected_task_text(store, d)
    )
    assert script.sent == []


def test_task_by_title_case_insensitive(store):
    d = seed_task_view(store)
    script = run_bot_until_stop(
        Script([[message_update(232, "/task yask WORKING")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent_rich) == 1
    assert (
        script.sent_rich[0]["rich_message"]["markdown"]
        == expected_task_text(store, d)
    )
    assert script.sent == []


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
    markdown = script.sent_rich[0]["rich_message"]["markdown"]
    assert markdown.startswith(f"# {t['number']} 2024 — Task\n")


def test_task_number_wins_over_same_title(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "2024")
    two = store.create_task(pid, "real two")
    script = run_bot_until_stop(
        Script([[message_update(239, "/task yask 2")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent_rich[0]["rich_message"]["markdown"].startswith(
        f"# {two['number']} real two — Task\n"
    )
    assert script.sent == []


def test_task_project_and_title_with_spaces(store):
    pid = store.create_project("my big project")["id"]
    store.create_task(pid, "other")
    t = store.create_task(pid, "fix the bug")
    script = run_bot_until_stop(
        Script([[message_update(240, "/task my big project fix the bug")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # longest-prefix project resolution: "my big project" + title "fix the bug"
    assert script.sent_rich[0]["rich_message"]["markdown"].startswith(
        f"# {t['number']} fix the bug — Task\n"
    )
    assert script.sent == []


def test_task_archived_by_number_not_by_title(store):
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "doomed")
    store.archive_task(pid, t["number"], confirm=True)
    script = run_bot_until_stop(
        Script([[message_update(241, f"/task yask {t['number']}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    markdown = script.sent_rich[0]["rich_message"]["markdown"]
    assert markdown.startswith(f"# {t['number']} doomed — Task\n")
    assert "**State:** Archived" in markdown
    assert script.sent == []
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
    assert script.sent_rich[0]["rich_message"]["markdown"].startswith(
        f"# {t['number']} working — Task\n"
    )
    assert script.sent == []


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
    store.create_task(pid, "chatty", description="x" * 31000)
    script = run_bot_until_stop(
        Script([[message_update(246, "/task yask chatty")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    markdown = script.sent_rich[0]["rich_message"]["markdown"]
    assert "… (truncated, 31000 chars total)" in markdown
    # capped at exactly DESCRIPTION_MAX description chars
    assert "x" * telegram_bot.DESCRIPTION_MAX in markdown
    assert "x" * (telegram_bot.DESCRIPTION_MAX + 1) not in markdown
    # the whole view stays under the Rich Message budget
    assert len(markdown) <= telegram_bot.RICH_MESSAGE_MAX
    assert script.sent == []


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
    lines = script.sent_rich[0]["rich_message"]["markdown"].split("\n")
    idx = lines.index("**History:**")
    assert lines[idx:] == (
        ["**History:**", f"- … {12 - telegram_bot.HISTORY_MAX} earlier transitions"]
        + [
            f"- {h['changed_at']} — {h['from_state']} → {h['to_state']} "
            f"({h['source']})"
            for h in history[-telegram_bot.HISTORY_MAX:]
        ]
    )
    assert script.sent == []


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
    # the /tasks list is a rich surface: the t: keyboard rides the rich
    # body (the cross-task contract: label "#<n> <title>", payload
    # "t:<pid>:<n>")
    rows = first.sent_rich[0]["reply_markup"]["inline_keyboard"]
    assert first.sent == []
    assert rows == [
        [
            {
                "text": f"#{t['number']} drill down",
                "callback_data": f"t:{pid}:{t['number']}",
            }
        ],
        [{"text": "Main menu", "callback_data": "h"}],
    ]
    payload = rows[0][0]["callback_data"]
    script = run_bot_until_stop(
        Script([[callback_update(249, payload, chat_id=11)]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the press is answered (no toast) and the detail view goes out as a
    # new Rich Message to the button's chat
    assert script.answered == [{"callback_query_id": "cbq-249"}]
    assert len(script.sent_rich) == 1
    assert script.sent_rich[0]["chat_id"] == 11
    markdown = script.sent_rich[0]["rich_message"]["markdown"]
    assert markdown.startswith(f"# {t['number']} drill down — Story\n")
    assert "**State:** In progress" in markdown
    assert "**Estimate:** 2" in markdown
    assert script.sent == []
    assert script.edited == []


def test_task_view_attachment_and_toggle_buttons(store):
    """The /task detail keyboard: attachment rows + state rows + toggle."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    n = t["number"]
    attachment_rows = [
        [{"text": "plan.md", "callback_data": f"a:{pid}:{n}:{d['plan']['id']}"}],
        [{"text": "img.png", "callback_data": f"a:{pid}:{n}:{d['img']['id']}"}],
    ]
    # 'working' is In progress: one button per other workflow state
    # (Backlog=0, Todo=1, Planning=2, Review=4, Done=5), 3 per row
    state_rows = [
        [
            {"text": "Backlog", "callback_data": f"m:{pid}:{n}:0"},
            {"text": "Todo", "callback_data": f"m:{pid}:{n}:1"},
            {"text": "Planning", "callback_data": f"m:{pid}:{n}:2"},
        ],
        [
            {"text": "Review", "callback_data": f"m:{pid}:{n}:4"},
            {"text": "Done", "callback_data": f"m:{pid}:{n}:5"},
        ],
    ]
    script = run_bot_until_stop(
        Script([[message_update(281, f"/task yask {n}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    sent = script.sent_rich[0]
    # the markdown is unchanged from the no-button form
    assert sent["rich_message"]["markdown"] == expected_task_text(store, d)
    assert sent["reply_markup"] == {
        "inline_keyboard": attachment_rows
        + state_rows
        + [[{"text": "Subscribe", "callback_data": f"s:{pid}"}]]
        + [[{"text": "Main menu", "callback_data": "h"}]]
    }
    assert script.sent == []
    # subscribing the chat flips only the toggle button
    store.subscribe_project(7, pid)
    script = run_bot_until_stop(
        Script([[message_update(282, f"/task yask {n}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    rows = script.sent_rich[0]["reply_markup"]["inline_keyboard"]
    assert rows[:2] == attachment_rows
    assert rows[2:4] == state_rows
    assert rows[4] == [{"text": "Unsubscribe", "callback_data": f"u:{pid}"}]
    assert rows[-1] == [{"text": "Main menu", "callback_data": "h"}]


def test_format_task_view_without_chat_is_plain_str(store):
    """No chat id → the plain markdown form as a str (no keyboard)."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    reply = telegram_bot.format_task_view(
        store.get_task(pid, t["number"]),
        store.get_project(pid),
        store.get_history(pid, t["number"]),
    )
    assert isinstance(reply, str)
    assert reply == expected_task_text(store, d)


def test_format_task_view_with_chat_is_rich_reply(store):
    """A chat id → a RichReply: the same markdown, the unchanged keyboard
    (attachment rows, state rows, toggle, Main menu)."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    n = t["number"]
    reply = telegram_bot.format_task_view(
        store.get_task(pid, n),
        store.get_project(pid),
        store.get_history(pid, n),
        chat_id=7,
        subscribed=False,
    )
    assert isinstance(reply, telegram_bot.RichReply)
    assert reply.markdown == expected_task_text(store, d)
    # 'working' is In progress: one button per other workflow state
    # (Backlog=0, Todo=1, Planning=2, Review=4, Done=5), 3 per row
    assert reply.reply_markup == {
        "inline_keyboard": [
            [{"text": "plan.md", "callback_data": f"a:{pid}:{n}:{d['plan']['id']}"}],
            [{"text": "img.png", "callback_data": f"a:{pid}:{n}:{d['img']['id']}"}],
            [
                {"text": "Backlog", "callback_data": f"m:{pid}:{n}:0"},
                {"text": "Todo", "callback_data": f"m:{pid}:{n}:1"},
                {"text": "Planning", "callback_data": f"m:{pid}:{n}:2"},
            ],
            [
                {"text": "Review", "callback_data": f"m:{pid}:{n}:4"},
                {"text": "Done", "callback_data": f"m:{pid}:{n}:5"},
            ],
            [{"text": "Subscribe", "callback_data": f"s:{pid}"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }


def test_task_view_unclosed_fence_at_cut_closes(store):
    """A truncation cut that leaves a code fence open appends the closing
    fence, so the note and the tail sections survive."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "fenced", description="```\n" + "x" * 40000)
    t = store.get_task(pid, 1)
    text = telegram_bot.format_task_view(
        t, store.get_project(pid), store.get_history(pid, 1)
    )
    assert isinstance(text, str)
    assert "… (truncated, 40004 chars total)" in text
    fences = [
        line for line in text.split("\n") if telegram_bot._FENCE_RE.match(line)
    ]
    # the cut's opening fence is closed: the count stays even
    assert len(fences) % 2 == 0
    assert "**History:**" in text


def test_format_task_view_minimal(store):
    """No estimate/parent/description/prerequisites/attachments → the
    heading, the State line and the History block only."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "bare")
    history = store.get_history(pid, t["number"])
    assert len(history) == 1
    text = telegram_bot.format_task_view(
        store.get_task(pid, t["number"]),
        store.get_project(pid),
        history,
    )
    assert isinstance(text, str)
    assert text == (
        f"# {t['number']} bare — Task\n"
        "**State:** Backlog\n"
        "\n"
        "**History:**\n"
        f"- {history[0]['changed_at']} — created (web)"
    )


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


# --- rich rendering over corpus-shaped markdown ------------------------------
# A representative slice of the real documents the bot renders as rich
# messages (the plan.md / session-summary.md / review.md / investigation.md
# attachments on epic #97 and its subtasks #99–#105): H1/H2 headings, a
# table with a |---| separator, - and 1. list items, a fenced code block
# with a language tag, inline #123-style task references, a line-leading
# #102 reference (a #tag heading-promotion candidate), a blockquote, a
# link, bold/italic/strike, and snake_case identifiers.
CORPUS_SHAPE_MD = """\
# Session summary — #103 Render small markdown attachments

**State:** In progress — *styling can shift* but ~~no~~ no content loss.

## What changed

- `attachment_reply`: now returns `Union[str, FileReply, RichReply]`
- The `_message_is_rich` presence check is shared with the re-render path
1. **Verify `Message.rich_message` is delivered in callback updates**
2. The flip is #106

| Component | Change |
|---|---|
| `RichMessageEdit` frozen dataclass (markdown + reply_markup) | L3166 |

#102 (rich `format_task_view`) are Done.

```python
def attachment_reply(meta, data, task):
    # The reply for one attachment.
    return RichReply(...)
```

> "Evidence (as of 2026-09-16): Telegram Web still cannot render rich
> messages and the failure is client-side."

See [the macOS forward/group bug](https://bugs.telegram.org/c/62896) for
details; inline refs like #123 and #93794 stay literal.
"""


def _structure_counts(md):
    """(headings, ul items, ol items, table rows, fence lines) in ``md``."""
    lines = md.split("\n")
    return (
        sum(1 for l in lines if re.match(r"^#{1,6} ", l)),
        sum(1 for l in lines if re.match(r"^[-*+]\s", l)),
        sum(1 for l in lines if re.match(r"^\d+[.)]\s", l)),
        sum(1 for l in lines if "|" in l),
        sum(1 for l in lines if l.startswith("```")),
    )


def test_rich_corpus_shape_attachment_surface():
    """Small markdown attachment → RichReply with the raw body intact."""
    meta = {
        "filename": "session-summary.md",
        "content_type": "text/markdown",
        "size": len(CORPUS_SHAPE_MD.encode("utf-8")),
    }
    task = {
        "number": 103,
        "title": "Render small markdown attachments as rich messages",
    }
    reply = telegram_bot.attachment_reply(
        meta, CORPUS_SHAPE_MD.encode("utf-8"), task
    )
    assert isinstance(reply, telegram_bot.RichReply)
    # no content loss: the raw body rides the payload untruncated
    assert CORPUS_SHAPE_MD in reply.markdown
    assert "… (truncated" not in reply.markdown
    # no dropped structure: the payload's counts are the body's plus the
    # #103 context line's (which carries no structural markers)
    context = f"#103 {task['title']} — session-summary.md"
    assert _structure_counts(reply.markdown) == tuple(
        a + b for a, b in zip(_structure_counts(CORPUS_SHAPE_MD),
                              _structure_counts(context))
    )


def test_rich_corpus_shape_task_view_surface():
    """The /task detail view with a corpus-shaped description → RichReply
    with the description intact (no truncation, no dropped structure)."""
    task = {
        "number": 103,
        "title": "Render small markdown attachments as rich messages",
        "type": "Task",
        "state": "In progress",
        "estimate": 2.0,
        "parent_number": 97,
        "description": CORPUS_SHAPE_MD,
        "prerequisites": [],
        "attachments": [],
    }
    project = {"name": "yask", "id": 1}
    reply = telegram_bot.format_task_view(task, project, [], chat_id=1)
    assert isinstance(reply, telegram_bot.RichReply)
    assert CORPUS_SHAPE_MD in reply.markdown
    assert "… (truncated" not in reply.markdown
    # the payload's structure counts are the body's plus the header block's
    # (one H1; the State/Estimate/Parent lines carry no structural markers)
    header = (
        f"# {task['number']} {task['title']} — {task['type']}\n"
        f"**State:** {task['state']}\n"
        f"**Estimate:** {task['estimate']:g}\n"
        f"**Parent:** #{task['parent_number']}"
    )
    assert _structure_counts(reply.markdown) == tuple(
        a + b for a, b in zip(_structure_counts(CORPUS_SHAPE_MD),
                              _structure_counts(header))
    )


def test_rich_corpus_shape_html_leg():
    """The HTML fallback leg of a corpus-shaped payload: no content lost,
    markers literal, only the supported tag set."""
    out = telegram_bot.markdown_to_html(CORPUS_SHAPE_MD)
    flat = html.unescape(re.sub(r"<[^>]+>", "", out))
    # the converter contract: only the supported tag set — never
    # h1–h6/ul/ol/li/p/br, which would 400 the whole leg
    tags = set(re.findall(r"</?([a-zA-Z][a-zA-Z0-9]*)", out))
    assert tags <= {
        "strong", "em", "del", "code", "pre", "a", "blockquote",
    }
    # the fence body lands in <pre><code> (the language tag is consumed)
    assert "<pre><code>def attachment_reply(meta, data, task):" in out
    # heading/list/table markers stay literal (the degraded leg carries
    # the structure in the markers, not in tags)
    assert "## What changed" in out
    assert "\n- attachment_reply" in flat
    assert "\n1. Verify Message.rich_message" in flat
    assert "| Component | Change |" in out
    assert "|---|---|" in out
    # the line-leading #102 reference and the inline refs stay literal
    assert "#102 (rich format_task_view) are Done." in flat
    assert "#123 and #93794 stay literal." in flat
    # snake_case identifiers are never emphasis (word-boundary guarded)
    assert "<code>_message_is_rich</code>" in out
    # the inline mappings
    assert "<strong>State:</strong>" in out
    assert "<em>styling can shift</em>" in out
    assert "<del>no</del>" in out
    assert '<a href="https://bugs.telegram.org/c/62896">' in out
    assert '<blockquote>&quot;Evidence (as of 2026-09-16):' in out
    # no non-blank line is dropped: every word of every line (fence
    # bodies checked against the <pre><code> blocks) survives in the
    # tag-stripped output
    pre_flat = html.unescape("\n".join(
        re.findall(r"<pre><code>(.*?)</code></pre>", out, re.S)
    ))
    in_code = False
    for line in CORPUS_SHAPE_MD.split("\n"):
        if line.startswith("```"):
            in_code = not in_code
            continue
        if not line.strip():
            continue
        target = pre_flat if in_code else flat
        for word in re.findall(
            r"[A-Za-z0-9_]{3,}", re.sub(r"https?://\S+", "", line)
        ):
            assert word in target, (
                f"word {word!r} from line {line[:60]!r} lost in the HTML leg"
            )


# --- markdown_to_html (HTML fallback converter) ------------------------------


def test_markdown_to_html_escapes_plain_text():
    assert (
        telegram_bot.markdown_to_html('a & b < c > d "e"')
        == "a &amp; b &lt; c &gt; d &quot;e&quot;"
    )


def test_markdown_to_html_escapes_inside_inline_code():
    assert (
        telegram_bot.markdown_to_html("use `a < b & c > d` here")
        == "use <code>a &lt; b &amp; c &gt; d</code> here"
    )


def test_markdown_to_html_escapes_inside_fence():
    assert (
        telegram_bot.markdown_to_html("```\n<script>alert(1)</script>\n```")
        == "<pre><code>&lt;script&gt;alert(1)&lt;/script&gt;</code></pre>"
    )


def test_markdown_to_html_script_stays_inert():
    out = telegram_bot.markdown_to_html("<script>alert('x')&</script> **b**")
    assert "<script" not in out
    assert "</script>" not in out


def test_markdown_to_html_link_text_and_href_escaped():
    assert (
        telegram_bot.markdown_to_html("[a & b](https://e.com/?x=1&y=2)")
        == '<a href="https://e.com/?x=1&amp;y=2">a &amp; b</a>'
    )


def test_markdown_to_html_link_href_cannot_break_out_of_quotes():
    out = telegram_bot.markdown_to_html('[x](https://e.com/"onerror=1)')
    assert out == '<a href="https://e.com/&quot;onerror=1">x</a>'
    assert out.count('"') == 2  # only the href's own quotes


def test_markdown_to_html_bold():
    assert telegram_bot.markdown_to_html("**b**") == "<strong>b</strong>"
    assert telegram_bot.markdown_to_html("__b__") == "<strong>b</strong>"


def test_markdown_to_html_italic():
    assert telegram_bot.markdown_to_html("*i*") == "<em>i</em>"
    assert telegram_bot.markdown_to_html("_i_") == "<em>i</em>"


def test_markdown_to_html_underscore_identifier_stays_literal():
    # the word-boundary guard: snake_case must never be italicized
    assert telegram_bot.markdown_to_html("a_b_c") == "a_b_c"
    assert telegram_bot.markdown_to_html("snake_case_name") == "snake_case_name"


def test_markdown_to_html_strike():
    assert telegram_bot.markdown_to_html("~~s~~") == "<del>s</del>"


def test_markdown_to_html_inline_code():
    assert telegram_bot.markdown_to_html("`c`") == "<code>c</code>"


def test_markdown_to_html_fence_multiline_newlines_preserved():
    assert (
        telegram_bot.markdown_to_html("```\na\nb\n```")
        == "<pre><code>a\nb</code></pre>"
    )


def test_markdown_to_html_fence_unclosed_at_eof():
    assert telegram_bot.markdown_to_html("```\nx") == "<pre><code>x</code></pre>"


def test_markdown_to_html_fence_empty_no_empty_tags():
    assert telegram_bot.markdown_to_html("```\n```") == ""


def test_markdown_to_html_quote():
    assert (
        telegram_bot.markdown_to_html("> q") == "<blockquote>q</blockquote>"
    )
    assert (
        telegram_bot.markdown_to_html(">no-space")
        == "<blockquote>no-space</blockquote>"
    )


def test_markdown_to_html_bare_quote_no_tag():
    assert "blockquote" not in telegram_bot.markdown_to_html(">")


def test_markdown_to_html_link():
    assert (
        telegram_bot.markdown_to_html("[x](https://e.com)")
        == '<a href="https://e.com">x</a>'
    )
    assert (
        telegram_bot.markdown_to_html("[x](http://e.com)")
        == '<a href="http://e.com">x</a>'
    )


def test_markdown_to_html_code_span_is_a_leaf():
    # no <a> inside <code> — the link markup stays literal
    assert (
        telegram_bot.markdown_to_html("`[x](https://e.com)`")
        == "<code>[x](https://e.com)</code>"
    )
    # no <strong> inside <code> — the emphasis markers stay literal
    assert telegram_bot.markdown_to_html("`**x**`") == "<code>**x**</code>"


def test_markdown_to_html_quote_context():
    assert (
        telegram_bot.markdown_to_html("> **b**")
        == "<blockquote><strong>b</strong></blockquote>"
    )
    # no <a> inside <blockquote> — the link stays literal
    assert (
        telegram_bot.markdown_to_html("> [x](https://e.com)")
        == "<blockquote>[x](https://e.com)</blockquote>"
    )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://files.example.com/x",
        "tg://user?id=1",
        "javascript:alert(1)",
        "//e.com/x",
        "e.com",
    ],
)
def test_markdown_to_html_link_scheme_http_https_only(url):
    assert telegram_bot.markdown_to_html(f"[x]({url})") == f"[x]({url})"


def test_markdown_to_html_headings_stay_literal():
    for level in range(1, 7):
        src = "#" * level + " H"
        assert telegram_bot.markdown_to_html(src) == src


def test_markdown_to_html_lists_stay_literal():
    for src in ["- item", "* item", "+ item", "1. item", "1) item"]:
        assert telegram_bot.markdown_to_html(src) == src
    # a trailing single asterisk in the item text is not em-wrapped
    assert telegram_bot.markdown_to_html("* item *") == "* item *"


def test_markdown_to_html_tables_and_dividers_stay_literal():
    assert telegram_bot.markdown_to_html("| a | b |") == "| a | b |"
    assert telegram_bot.markdown_to_html("---") == "---"


def test_markdown_to_html_link_text_may_carry_bold():
    assert (
        telegram_bot.markdown_to_html("[**x**](https://e.com)")
        == '<a href="https://e.com"><strong>x</strong></a>'
    )


def test_markdown_to_html_kitchen_sink_tag_set():
    src = (
        "# H1\n"
        "**bold** and *italic* and __ub__ and _ui_ and ~~gone~~\n"
        "`code` and [link](https://e.com)\n"
        "```\nfenced <script> &\n```\n"
        "> quote **b** and [x](https://e.com)\n"
        "- item *em*\n"
        "| t | a | b |\n"
        "---\n"
    )
    out = telegram_bot.markdown_to_html(src)
    tags = set(re.findall(r"</?([a-z][a-z0-9]*)", out))
    assert tags <= {"strong", "em", "del", "code", "pre", "a", "blockquote"}


def test_markdown_to_html_crlf_normalized_like_lf():
    assert (
        telegram_bot.markdown_to_html("a\r\n\r\nb")
        == telegram_bot.markdown_to_html("a\n\nb")
    )


def test_markdown_to_html_blank_lines_preserved():
    assert telegram_bot.markdown_to_html("a\n\nb") == "a\n\nb"


def test_markdown_to_html_empty_and_none():
    assert telegram_bot.markdown_to_html("") == ""
    assert telegram_bot.markdown_to_html(None) == ""


# --- /attachment (store-backed dispatch) -------------------------------------


def test_attachment_small_markdown_rendered_inline(store):
    """A <16 KB markdown attachment goes out as a Rich Message — the full
    7800-char seed body, no truncation."""
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
    assert script.sent_files == []  # inline rich, not a file
    assert script.sent == []  # a Rich Message, not sendMessage
    assert len(script.sent_rich) == 1
    assert script.sent_rich[0]["chat_id"] == 7
    assert (
        script.sent_rich[0]["rich_message"]["markdown"]
        == expected_rich_markdown(
            d["t"]["number"], "working", "plan.md", d["plan_bytes"].decode()
        )
    )


def test_attachment_large_markdown_sent_as_document(store):
    """A >=16 KB markdown attachment is still sent as a document."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    big = b"# Big\n" + b"y" * (17 * 1024)  # 17 KB, over the inline threshold
    a = store.add_attachment(pid, t["number"], "big.md", "text/markdown", big)
    script = run_bot_until_stop(
        Script([[message_update(275, f"/attachment yask {t['number']} {a['id']}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent == []  # a file, not a text message
    assert len(script.sent_files) == 1
    f = script.sent_files[0]
    assert f["method"] == "sendDocument"
    assert f["chat_id"] == 7
    assert f["filename"] == "big.md"
    assert f["data"] == big
    assert f["caption"] == f"#{t['number']} working — big.md"


def test_attachment_plain_inline_truncated_to_message_cap(store):
    """A <16 KB plain text attachment over the message cap is still
    truncated with a note — the plain path's cap is untouched by the
    rich change."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    body = b"#" + b"m" * 4999  # 5000 chars — over INLINE_TEXT_MAX, under 16 KB
    a = store.add_attachment(pid, t["number"], "long.txt", "text/plain", body)
    script = run_bot_until_stop(
        Script(
            [[message_update(276, f"/attachment yask {t['number']} {a['id']}")]],
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent_rich == []  # plain text is not rich-formatted
    assert script.sent_files == []
    assert len(script.sent) == 1
    assert "parse_mode" not in script.sent[0]
    text = script.sent[0]["text"]
    assert len(text) <= telegram_bot.INLINE_TEXT_MAX
    assert text.startswith(f"#{t['number']} working — long.txt\n")
    assert text.endswith("\n… (truncated, 5000 chars total)")
    assert text == expected_inline_text(
        t["number"], "working", "long.txt", body.decode()
    )


def test_attachment_small_plain_text_stays_plain(store):
    """A <16 KB text/plain attachment is sent as plain text as before
    (plain text is deliberately not formatted)."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    body = b"plain body line 1\nplain body line 2"
    a = store.add_attachment(pid, t["number"], "notes.txt", "text/plain", body)
    script = run_bot_until_stop(
        Script(
            [[message_update(277, f"/attachment yask {t['number']} {a['id']}")]],
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent_rich == []
    assert script.sent_files == []
    assert len(script.sent) == 1
    sent = script.sent[0]
    assert sent["chat_id"] == 7
    assert "parse_mode" not in sent
    assert sent["text"] == expected_inline_text(
        t["number"], "working", "notes.txt", body.decode()
    )


def test_attachment_inline_boundary(store):
    """The inline threshold is strict: 16383 bytes is a Rich Message,
    exactly 16384 a document."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    just_under = b"x" * 16383
    a = store.add_attachment(pid, t["number"], "edge.md", "text/markdown", just_under)
    script = run_bot_until_stop(
        Script(
            [[message_update(278, f"/attachment yask {t['number']} {a['id']}")]],
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent_files == []
    assert script.sent == []
    assert len(script.sent_rich) == 1
    assert (
        script.sent_rich[0]["rich_message"]["markdown"]
        == expected_rich_markdown(
            t["number"], "working", "edge.md", just_under.decode()
        )
    )
    exactly = b"y" * 16384
    b2 = store.add_attachment(pid, t["number"], "edge2.md", "text/markdown", exactly)
    script = run_bot_until_stop(
        Script(
            [[message_update(279, f"/attachment yask {t['number']} {b2['id']}")]],
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent_rich == []
    assert script.sent == []
    assert len(script.sent_files) == 1
    f = script.sent_files[0]
    assert f["method"] == "sendDocument"
    assert f["chat_id"] == 7
    assert f["filename"] == "edge2.md"
    assert f["data"] == exactly
    assert f["caption"] == f"#{t['number']} working — edge2.md"


def test_attachment_rich_fallback_to_html(store):
    """A failing rich leg degrades through the shared funnel: the HTML leg
    carries the markdown re-truncated to the regular budget, converted."""
    d = seed_task_view(store)
    script = run_bot_until_stop(
        Script(
            [
                [
                    message_update(
                        280, f"/attachment yask {d['t']['number']} {d['plan']['id']}"
                    )
                ]
            ],
            fail_rich_once=(400, "can't parse rich markdown"),
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    # the rich leg fired (and failed) exactly once, full payload
    assert len(script.sent_rich) == 1
    markdown = expected_rich_markdown(
        d["t"]["number"], "working", "plan.md", d["plan_bytes"].decode()
    )
    assert script.sent_rich[0]["rich_message"]["markdown"] == markdown
    # the HTML leg replaced it — the markdown re-truncated to the regular
    # budget with the note, converted, not raw-sent
    assert len(script.sent) == 1
    body = script.sent[0]
    assert body["chat_id"] == 7
    assert body["parse_mode"] == "HTML"
    truncated = telegram_bot._truncate_inline(
        markdown, len(markdown), telegram_bot.REGULAR_TEXT_MAX
    )
    assert len(truncated) <= telegram_bot.REGULAR_TEXT_MAX
    assert truncated.endswith(
        f"\n… (truncated, {len(markdown)} chars total)"
    )
    assert body["text"] == telegram_bot.markdown_to_html(truncated)


def test_attachment_rich_kill_switch_off(store):
    """With rich disabled the markdown attachment takes the HTML leg
    first — no sendRichMessage call (the epic's degradation)."""
    d = seed_task_view(store)
    script = run_bot_until_stop(
        Script(
            [
                [
                    message_update(
                        281, f"/attachment yask {d['t']['number']} {d['plan']['id']}"
                    )
                ]
            ],
            fail_rich_once=(400, "should never be called"),
        ),
        dispatch=telegram_bot.make_dispatch(store),
        rich=False,
    )
    assert script.sent_rich == []
    assert script.sent_files == []
    assert len(script.sent) == 1
    body = script.sent[0]
    assert body["chat_id"] == 7
    assert body["parse_mode"] == "HTML"
    markdown = expected_rich_markdown(
        d["t"]["number"], "working", "plan.md", d["plan_bytes"].decode()
    )
    truncated = telegram_bot._truncate_inline(
        markdown, len(markdown), telegram_bot.REGULAR_TEXT_MAX
    )
    assert body["text"] == telegram_bot.markdown_to_html(truncated)


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
    """Task resolution by title still works — the small markdown
    attachment goes out as a Rich Message."""
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
    assert script.sent_files == []
    assert script.sent == []
    assert len(script.sent_rich) == 1
    assert (
        script.sent_rich[0]["rich_message"]["markdown"]
        == expected_rich_markdown(
            d["t"]["number"], "working", "plan.md", d["plan_bytes"].decode()
        )
    )


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


# --- /move (store-backed dispatch) -------------------------------------------


def test_move_usage_texts(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "working")
    for text in ("/move", "/move yask"):
        script = run_bot_until_stop(
            Script([[message_update(701, text)]]),
            dispatch=telegram_bot.make_dispatch(store),
        )
        assert script.sent[0]["text"] == telegram_bot.MOVE_USAGE_TEXT


def test_move_single_task_applies_and_confirms_in_text(store):
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    script = run_bot_until_stop(
        Script([[message_update(702, f"/move yask {t['number']} Review")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["text"] == f"Moved #{t['number']} to Review."
    assert "reply_markup" not in script.sent[0]
    assert store.get_task(pid, t["number"])["state"] == "Review"


def test_move_multiword_state_with_number(store):
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    script = run_bot_until_stop(
        Script([[message_update(703, f"/move yask {t['number']} In progress")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == f"Moved #{t['number']} to In progress."
    assert store.get_task(pid, t["number"])["state"] == "In progress"


def test_move_multiword_title_and_state(store):
    """'/move yask fix the bug Review' → task 'fix the bug', state 'Review'."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "fix the bug")
    script = run_bot_until_stop(
        Script([[message_update(704, "/move yask fix the bug Review")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == f"Moved #{t['number']} to Review."
    assert store.get_task(pid, t["number"])["state"] == "Review"


def test_move_state_case_insensitive(store):
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    script = run_bot_until_stop(
        Script([[message_update(705, f"/move yask {t['number']} dOnE")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == f"Moved #{t['number']} to Done."
    assert store.get_task(pid, t["number"])["state"] == "Done"


def test_move_unknown_state_lists_workflow_states(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "working")
    script = run_bot_until_stop(
        Script([[message_update(706, "/move yask 1 Shipping")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Unknown state 'Shipping'. Use one of: "
        "Backlog, Todo, Planning, In progress, Review, Done."
    )


def test_move_state_only_argument_never_matches(store):
    """A bare state is not a (task, state) pair: it gets the unknown-state
    reply, not a usage or not-found reply."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "working")
    script = run_bot_until_stop(
        Script([[message_update(707, "/move yask Review")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Unknown state 'Review'. Use one of: "
        "Backlog, Todo, Planning, In progress, Review, Done."
    )


def test_move_already_in_state(store):
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    store.move_task(pid, t["number"], "Review", confirm=True)
    script = run_bot_until_stop(
        Script([[message_update(708, f"/move yask {t['number']} Review")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == f"Task #{t['number']} is already in Review."


def test_move_archived_task_refused(store):
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    store.archive_task(pid, t["number"], confirm=True)
    script = run_bot_until_stop(
        Script([[message_update(709, f"/move yask {t['number']} Review")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        f"Task #{t['number']} is archived and cannot be moved."
    )
    assert store.get_task(pid, t["number"])["state"] == "Archived"


def test_move_unknown_task_and_project(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "working")
    script = run_bot_until_stop(
        Script([[message_update(711, "/move yask 99 Review")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == "Task #99 not found in yask."
    script = run_bot_until_stop(
        Script([[message_update(712, "/move nope 1 Review")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Project 'nope' not found. Use /projects to list projects."
    )


def test_move_cascade_gets_confirm_keyboard(store):
    """A move that pulls prerequisites replies with the confirm keyboard —
    nothing is written until the c: button is pressed."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    # 'working' (In progress) has 'prereq one' (Review) and 'prereq two'
    # (In progress) as prerequisites — both pull to Done
    script = run_bot_until_stop(
        Script([[message_update(713, f"/move yask {t['number']} Done")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    sent = script.sent[0]
    assert sent["text"] == (
        f"Move #{t['number']} to Done?\n"
        "This also moves its prerequisites that have not reached this stage:\n"
        f"  #{d['p1']['number']} prereq one — Review\n"
        f"  #{d['p2']['number']} prereq two — In progress"
    )
    assert sent["reply_markup"] == {
        "inline_keyboard": [
            [
                {
                    "text": "Move all",
                    "callback_data": f"c:{pid}:{t['number']}:5",
                }
            ],
            [
                {
                    "text": "Cancel",
                    "callback_data": f"x:{pid}:{t['number']}",
                }
            ],
        ]
    }
    # nothing has moved yet
    assert store.get_task(pid, t["number"])["state"] == "In progress"
    assert store.get_task(pid, d["p1"]["number"])["state"] == "Review"
    assert store.get_task(pid, d["p2"]["number"])["state"] == "In progress"


def test_move_store_failure_replies_and_recovers(store, monkeypatch):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "working")

    def boom(project_id, number, to_state):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "plan_move", boom)
    script = run_bot_until_stop(
        Script(
            [
                [
                    message_update(714, "/move yask 1 Review"),
                    message_update(715, "/start"),
                ]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == telegram_bot.MOVE_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT


def test_help_mentions_move():
    assert "/move" in telegram_bot.HELP_TEXT


# --- /add (store-backed dispatch) --------------------------------------------


def test_add_task_creates_task_in_backlog(store):
    pid = store.create_project("yask")["id"]
    script = run_bot_until_stop(
        Script([[message_update(720, "/add yask fix the bug")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 fix the bug", "callback_data": f"t:{pid}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }
    assert script.sent[0]["text"] == (
        "Created #1 'fix the bug' (Task) in yask — Backlog. "
        "Use /task yask 1 to view it."
    )
    tasks = store.list_tasks(pid)
    assert len(tasks) == 1
    assert tasks[0]["number"] == 1
    assert tasks[0]["title"] == "fix the bug"
    assert tasks[0]["state"] == "Backlog"
    assert tasks[0]["type"] == "Task"


def test_add_reply_button_opens_task_view(store):
    """Pressing the /add confirmation's button must open the new task's
    detail view (the ``t:`` family, as with /tasks and the
    notifications)."""
    pid = store.create_project("yask")["id"]
    first = run_bot_until_stop(
        Script([[message_update(721, "/add yask fix the bug")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    rows = first.sent[0]["reply_markup"]["inline_keyboard"]
    assert rows == [
        [{"text": "#1 fix the bug", "callback_data": f"t:{pid}:1"}],
        [{"text": "Main menu", "callback_data": "h"}],
    ]
    payload = rows[0][0]["callback_data"]
    script = run_bot_until_stop(
        Script([[callback_update(723, payload, chat_id=11)]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the press is answered (no toast) and the detail view goes out as a
    # new Rich Message to the pressing chat
    assert script.answered == [{"callback_query_id": "cbq-723"}]
    assert len(script.sent_rich) == 1
    assert script.sent_rich[0]["chat_id"] == 11
    markdown = script.sent_rich[0]["rich_message"]["markdown"]
    assert markdown.startswith("# 1 fix the bug — Task\n")
    assert "**State:** Backlog" in markdown
    assert script.sent == []
    assert script.edited == []


def test_add_reply_button_label_truncated(store):
    """A title beyond the cap truncates the button label; the message
    text keeps the full title."""
    pid = store.create_project("yask")["id"]
    title = "a" * 100
    script = run_bot_until_stop(
        Script([[message_update(725, f"/add yask {title}")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    rows = script.sent[0]["reply_markup"]["inline_keyboard"]
    label = rows[0][0]["text"]
    assert label == telegram_bot._truncate_button_label(f"#1 {title}")
    assert len(label) <= telegram_bot.NOTIFICATION_BUTTON_TEXT_MAX
    assert title in script.sent[0]["text"]


def test_add_task_by_project_id(store):
    pid = store.create_project("yask")["id"]
    script = run_bot_until_stop(
        Script([[message_update(722, f"/add {pid} new thing")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Created #1 'new thing' (Task) in yask — Backlog. "
        "Use /task yask 1 to view it."
    )
    assert store.get_task(pid, 1)["title"] == "new thing"


def test_add_task_project_and_title_with_spaces(store):
    """Longest-prefix resolution: project 'my big project', title
    'fix the bug'."""
    pid = store.create_project("my big project")["id"]
    script = run_bot_until_stop(
        Script([[message_update(724, "/add my big project fix the bug")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Created #1 'fix the bug' (Task) in my big project — Backlog. "
        "Use /task my big project 1 to view it."
    )
    t = store.get_task(pid, 1)
    assert t["title"] == "fix the bug"
    assert t["state"] == "Backlog"
    assert t["type"] == "Task"


def test_add_task_with_type_suffix(store):
    """The trailing ``as <type>`` segment names the type (case-insensitive
    on both the keyword and the type name)."""
    pid = store.create_project("yask")["id"]
    script = run_bot_until_stop(
        Script(
            [
                [
                    message_update(740, "/add yask fix the login bug as Bug"),
                    message_update(741, "/add yask fix the crash as bug"),
                ]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == (
        "Created #1 'fix the login bug' (Bug) in yask — Backlog. "
        "Use /task yask 1 to view it."
    )
    assert script.sent[1]["text"] == (
        "Created #2 'fix the crash' (Bug) in yask — Backlog. "
        "Use /task yask 2 to view it."
    )
    tasks = store.list_tasks(pid)
    assert [t["title"] for t in tasks] == ["fix the login bug", "fix the crash"]
    assert [t["type"] for t in tasks] == ["Bug", "Bug"]
    assert [t["state"] for t in tasks] == ["Backlog", "Backlog"]


def test_add_task_with_multicustom_type(store):
    """Multi-word custom type names work via the ``as <type>`` segment."""
    pid = store.create_project("yask")["id"]
    store.create_task_type("Code Review")
    script = run_bot_until_stop(
        Script([[message_update(742, "/add yask check the diff as Code Review")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Created #1 'check the diff' (Code Review) in yask — Backlog. "
        "Use /task yask 1 to view it."
    )
    t = store.get_task(pid, 1)
    assert t["title"] == "check the diff"
    assert t["type"] == "Code Review"
    assert t["state"] == "Backlog"


def test_add_task_as_epic(store):
    """``as Epic`` creates an Epic (a root task, Backlog, unestimated)."""
    pid = store.create_project("yask")["id"]
    script = run_bot_until_stop(
        Script([[message_update(743, "/add yask rollup as Epic")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 rollup", "callback_data": f"t:{pid}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }
    assert script.sent[0]["text"] == (
        "Created #1 'rollup' (Epic) in yask — Backlog. "
        "Use /task yask 1 to view it."
    )
    t = store.get_task(pid, 1)
    assert t["title"] == "rollup"
    assert t["type"] == "Epic"
    assert t["is_epic"] is True
    assert t["estimate"] is None
    assert t["state"] == "Backlog"


def test_add_task_as_not_a_type_keeps_title(store):
    """A trailing ``as <word>`` that is not a type name stays in the title
    (the task is a plain Task)."""
    pid = store.create_project("yask")["id"]
    script = run_bot_until_stop(
        Script([[message_update(744, "/add yask fix the bug as mentioned earlier")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Created #1 'fix the bug as mentioned earlier' (Task) in yask — Backlog. "
        "Use /task yask 1 to view it."
    )
    t = store.get_task(pid, 1)
    assert t["title"] == "fix the bug as mentioned earlier"
    assert t["type"] == "Task"


def test_add_task_multiple_as_last_wins(store):
    """Only the last ``as`` is a split point: ``a as b as Bug`` → Bug
    titled ``a as b``."""
    pid = store.create_project("yask")["id"]
    script = run_bot_until_stop(
        Script([[message_update(745, "/add yask a as b as Bug")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Created #1 'a as b' (Bug) in yask — Backlog. "
        "Use /task yask 1 to view it."
    )
    t = store.get_task(pid, 1)
    assert t["title"] == "a as b"
    assert t["type"] == "Bug"


def test_add_task_type_only_no_title(store):
    """``/add yask as Bug`` has a type but no title: usage text, nothing
    is created."""
    pid = store.create_project("yask")["id"]
    script = run_bot_until_stop(
        Script([[message_update(746, "/add yask as Bug")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == telegram_bot.add_usage_text(store)
    assert store.list_tasks(pid) == []


def test_match_type_suffix_edge_cases():
    """Direct unit checks for :func:`telegram_bot._match_type_suffix`."""
    types = ["Story", "Task", "Bug", "Epic"]
    # No "as": the whole words are the title.
    assert telegram_bot._match_type_suffix(
        ["fix", "the", "bug"], types
    ) == (["fix", "the", "bug"], None)
    # Trailing "as" with nothing after it: legacy interpretation.
    assert telegram_bot._match_type_suffix(
        ["fix", "as"], types
    ) == (["fix", "as"], None)
    # A known type: split, the canonical name is returned.
    assert telegram_bot._match_type_suffix(
        ["fix", "the", "bug", "as", "Bug"], types
    ) == (["fix", "the", "bug"], "Bug")
    # Case-insensitive on both the keyword and the type name.
    assert telegram_bot._match_type_suffix(
        ["fix", "AS", "bUG"], types
    ) == (["fix"], "Bug")
    # A segment that is not a type: the whole words are the title.
    assert telegram_bot._match_type_suffix(
        ["fix", "as", "mentioned"], types
    ) == (["fix", "as", "mentioned"], None)
    # Only the last "as" counts.
    assert telegram_bot._match_type_suffix(
        ["a", "as", "b", "as", "Bug"], types
    ) == (["a", "as", "b"], "Bug")
    # A multi-word type name.
    assert telegram_bot._match_type_suffix(
        ["x", "as", "Code", "Review"], ["Code Review"]
    ) == (["x"], "Code Review")
    # A type with no title words ("as Bug" alone).
    assert telegram_bot._match_type_suffix(["as", "Bug"], types) == ([], "Bug")


def test_add_usage_and_not_found(store):
    pid = store.create_project("yask")["id"]
    script = run_bot_until_stop(
        Script(
            [
                [message_update(726, "/add")],
                [message_update(727, "/add yask")],
                [message_update(729, "/add nope thing")],
                [message_update(731, "/add 999 thing")],
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    usage = telegram_bot.add_usage_text(store)
    assert len(script.sent) == 4
    assert script.sent[0]["text"] == usage
    assert script.sent[1]["text"] == usage
    assert script.sent[2]["text"] == (
        "Project 'nope' not found. Use /projects to list projects."
    )
    assert script.sent[3]["text"] == (
        "Project '999' not found. Use /projects to list projects."
    )
    assert store.list_tasks(pid) == []


def test_add_store_failure_replies_and_recovers(store, monkeypatch):
    pid = store.create_project("yask")["id"]

    def boom(project_id, title, *args, **kwargs):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "create_task", boom)
    script = run_bot_until_stop(
        Script(
            [
                [
                    message_update(733, "/add yask new thing"),
                    message_update(734, "/start"),
                ]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == telegram_bot.ADD_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT
    assert store.list_tasks(pid) == []


def test_help_mentions_add():
    assert "/add" in telegram_bot.HELP_TEXT


# --- /describe (store-backed dispatch, write) ---------------------------------


def test_describe_number_form_sets_and_overwrites(store):
    """The number form sets the description; a second call replaces it
    (replace, not append) and the new text shows up in the /task view."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    script = run_bot_until_stop(
        Script(
            [
                [message_update(750, "/describe yask 1 first description")],
                [message_update(751, "/describe yask 1 second description")],
                [message_update(752, "/task yask 1")],
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 2
    # the confirmation carries the exact two-row keyboard (the #92 shape)
    assert script.sent[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 the bug", "callback_data": f"t:{pid}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }
    assert script.sent[0]["text"] == (
        f"Set the description of #1 'the bug' "
        f"({len('first description')} chars). "
        "Use /task yask 1 to view it."
    )
    assert script.sent[1]["text"] == (
        f"Set the description of #1 'the bug' "
        f"({len('second description')} chars). "
        "Use /task yask 1 to view it."
    )
    # replace semantics: the second call overwrote the first
    assert store.get_task(pid, 1)["description"] == "second description"
    # and the /task detail view (a Rich Message) shows the new text, not
    # the old one — the description is embedded raw, with no label
    assert len(script.sent_rich) == 1
    markdown = script.sent_rich[0]["rich_message"]["markdown"]
    assert "second description" in markdown
    assert "first description" not in markdown


def test_describe_title_form_unique_resolves(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    script = run_bot_until_stop(
        Script([[message_update(753, "/describe yask the bug some notes")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["text"] == (
        "Set the description of #1 'the bug' (10 chars). "
        "Use /task yask 1 to view it."
    )
    assert store.get_task(pid, 1)["description"] == "some notes"


def test_describe_title_form_longest_prefix_wins(store):
    """Titles 'fix bug' and 'fix bug today': '/describe ... fix bug later
    today' splits at the longest prefix with a unique match ('fix bug'),
    leaving 'later today' as the description."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "fix bug")
    store.create_task(pid, "fix bug today")
    script = run_bot_until_stop(
        Script(
            [[message_update(754, "/describe yask fix bug later today")]],
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["text"] == (
        "Set the description of #1 'fix bug' (11 chars). "
        "Use /task yask 1 to view it."
    )
    assert store.get_task(pid, 1)["description"] == "later today"
    assert store.get_task(pid, 2)["description"] == ""


def test_describe_ambiguous_title_disambiguates(store):
    """Two equal titles: the disambiguation list, nothing is written."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "dup")
    store.create_task(pid, "dup")
    script = run_bot_until_stop(
        Script([[message_update(755, "/describe yask dup hello world")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["text"] == (
        "Several tasks in yask match 'dup':\n"
        "  #1 dup — Backlog\n"
        "  #2 dup — Backlog\n"
        "Use /task yask <number>."
    )
    assert "reply_markup" not in script.sent[0]
    assert store.get_task(pid, 1)["description"] == ""
    assert store.get_task(pid, 2)["description"] == ""


def test_describe_usage_and_not_found(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    script = run_bot_until_stop(
        Script(
            [
                [message_update(756, "/describe")],
                [message_update(757, "/describe yask")],
                [message_update(758, "/describe yask the bug")],
                [message_update(759, "/describe nope thing")],
                [message_update(760, "/describe yask 999 hello")],
                [message_update(761, "/describe yask no such task hello")],
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 6
    assert script.sent[0]["text"] == telegram_bot.DESCRIBE_USAGE_TEXT
    assert script.sent[1]["text"] == telegram_bot.DESCRIBE_USAGE_TEXT
    # a resolved reference with no description words left is also a usage
    assert script.sent[2]["text"] == telegram_bot.DESCRIBE_USAGE_TEXT
    assert script.sent[3]["text"] == (
        "Project 'nope' not found. Use /projects to list projects."
    )
    assert script.sent[4]["text"] == "Task #999 not found in yask."
    # the title form quotes the first word (the project-not-found convention)
    assert script.sent[5]["text"] == "Task 'no' not found in yask."
    # nothing was written
    assert store.get_task(pid, 1)["description"] == ""


def test_describe_requires_auth(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    store.add_telegram_user(7, "pw")
    auth = telegram_bot.Auth(store)
    script = run_bot_until_stop(
        Script([[message_update(762, "/describe yask 1 secret")]]),
        dispatch=telegram_bot.make_dispatch(store, auth),
    )
    assert script.sent[0]["text"] == telegram_bot.AUTH_REQUIRED_TEXT
    assert store.get_task(pid, 1)["description"] == ""


def test_describe_store_failure_replies_and_recovers(store, monkeypatch):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")

    def boom(project_id, number, **kwargs):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "update_task", boom)
    script = run_bot_until_stop(
        Script(
            [
                [
                    message_update(763, "/describe yask 1 hello"),
                    message_update(764, "/start"),
                ]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == telegram_bot.DESCRIBE_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT
    assert store.get_task(pid, 1)["description"] == ""


def test_help_mentions_describe():
    assert "/describe" in telegram_bot.HELP_TEXT


# --- /type (store-backed dispatch, write) ------------------------------------


def test_type_number_form_changes_type(store):
    """The number form changes the type (store state verified via get_task);
    the confirmation carries the detail button plus the Main-menu row."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    script = run_bot_until_stop(
        Script([[message_update(770, "/type yask 1 Bug")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 the bug", "callback_data": f"t:{pid}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }
    assert script.sent[0]["text"] == (
        "Changed the type of #1 'the bug' to Bug. Use /task yask 1 to view it."
    )
    assert store.get_task(pid, 1)["type"] == "Bug"


def test_type_number_form_unknown_number(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    script = run_bot_until_stop(
        Script([[message_update(771, "/type yask 999 Bug")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == "Task #999 not found in yask."
    assert "reply_markup" not in script.sent[0]
    assert store.get_task(pid, 1)["type"] == "Task"


def test_type_title_form_unique_resolves(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    script = run_bot_until_stop(
        Script([[message_update(772, "/type yask the bug Story")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Changed the type of #1 'the bug' to Story. Use /task yask 1 to view it."
    )
    assert store.get_task(pid, 1)["type"] == "Story"


def test_type_title_form_longest_prefix_wins(store):
    """Titles 'fix bug' and 'fix bug story': '/type ... fix bug story
    Story' splits at the longest prefix with a unique match ('fix bug
    story' — a task whose tail words spell a type name), leaving 'Story'
    as the type (the /describe convention)."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "fix bug")
    store.create_task(pid, "fix bug story")
    script = run_bot_until_stop(
        Script([[message_update(773, "/type yask fix bug story Story")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Changed the type of #2 'fix bug story' to Story. "
        "Use /task yask 2 to view it."
    )
    assert store.get_task(pid, 1)["type"] == "Task"
    assert store.get_task(pid, 2)["type"] == "Story"


def test_type_ambiguous_title_disambiguates(store):
    """Two equal titles: the disambiguation list, nothing is written."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "dup")
    store.create_task(pid, "dup")
    script = run_bot_until_stop(
        Script([[message_update(774, "/type yask dup Bug")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Several tasks in yask match 'dup':\n"
        "  #1 dup — Backlog\n"
        "  #2 dup — Backlog\n"
        "Use /task yask <number>."
    )
    assert "reply_markup" not in script.sent[0]
    assert store.get_task(pid, 1)["type"] == "Task"
    assert store.get_task(pid, 2)["type"] == "Task"


def test_type_unknown_type_lists_board_types(store):
    """The error lists the board's current types — including a custom type
    created via store.create_task_type (the list is board-driven)."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    store.create_task_type("Investigation")
    script = run_bot_until_stop(
        Script([[message_update(775, "/type yask 1 Nope")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Unknown type 'Nope'. "
        "Use one of: Epic, Bug, Investigation, Story, Task."
    )
    assert "reply_markup" not in script.sent[0]
    assert store.get_task(pid, 1)["type"] == "Task"


def test_type_case_insensitive_type_match(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    script = run_bot_until_stop(
        Script([[message_update(776, "/type yask 1 bUg")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Changed the type of #1 'the bug' to Bug. Use /task yask 1 to view it."
    )
    assert store.get_task(pid, 1)["type"] == "Bug"


def test_type_same_type_replies_already(store):
    """Naming the task's current type (case-insensitively): a reply,
    nothing written."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug", type="Bug")
    script = run_bot_until_stop(
        Script([[message_update(777, "/type yask 1 bug")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == "Task #1 is already of type Bug."
    assert "reply_markup" not in script.sent[0]
    assert store.get_task(pid, 1)["type"] == "Bug"


def test_type_usage_and_not_found(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    script = run_bot_until_stop(
        Script(
            [
                [message_update(778, "/type")],
                [message_update(779, "/type yask")],
                [message_update(780, "/type yask the bug")],
                [message_update(781, "/type yask 1")],
                [message_update(782, "/type nope thing")],
                [message_update(783, "/type yask 999 Bug")],
                [message_update(784, "/type yask no such task Bug")],
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    types = ", ".join(t["name"] for t in store.list_task_types())
    usage = (
        "Usage: /type <project> <number|title> <type>\n"
        f"Changes the task's type; type is one of: {types}.\n"
        "Example: /type yask 4 Bug"
    )
    assert len(script.sent) == 7
    assert script.sent[0]["text"] == usage
    assert script.sent[1]["text"] == usage
    # a resolved reference with no type words left is also a usage
    assert script.sent[2]["text"] == usage
    assert script.sent[3]["text"] == usage
    assert script.sent[4]["text"] == (
        "Project 'nope' not found. Use /projects to list projects."
    )
    assert script.sent[5]["text"] == "Task #999 not found in yask."
    # the title form quotes the first word (the /describe convention)
    assert script.sent[6]["text"] == "Task 'no' not found in yask."
    # nothing was written
    assert store.get_task(pid, 1)["type"] == "Task"


def test_type_epic_with_children_cannot_be_demoted(store):
    """Demoting an epic that has children: the store's ValidationError
    message is surfaced verbatim, the type is unchanged."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the epic", type="Epic")
    store.create_task(pid, "child", parent_number=1)
    script = run_bot_until_stop(
        Script([[message_update(785, "/type yask 1 Task")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "task has children and cannot be changed away from an epic"
    )
    assert "reply_markup" not in script.sent[0]
    assert store.get_task(pid, 1)["type"] == "Epic"


def test_type_to_epic_nulls_estimate(store):
    """Switching a regular task with an estimate to Epic: the estimate is
    nulled (store behavior, pinned through the bot path)."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "big thing", estimate=3)
    script = run_bot_until_stop(
        Script([[message_update(786, "/type yask 1 Epic")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == (
        "Changed the type of #1 'big thing' to Epic. Use /task yask 1 to view it."
    )
    task = store.get_task(pid, 1)
    assert task["type"] == "Epic"
    assert task["estimate"] is None


def test_type_requires_auth(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    store.add_telegram_user(7, "pw")
    auth = telegram_bot.Auth(store)
    script = run_bot_until_stop(
        Script([[message_update(787, "/type yask 1 Bug")]]),
        dispatch=telegram_bot.make_dispatch(store, auth),
    )
    assert script.sent[0]["text"] == telegram_bot.AUTH_REQUIRED_TEXT
    assert store.get_task(pid, 1)["type"] == "Task"


def test_type_store_failure_replies_and_recovers(store, monkeypatch):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")

    def boom(project_id, number, **kwargs):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "update_task", boom)
    script = run_bot_until_stop(
        Script(
            [
                [
                    message_update(788, "/type yask 1 Bug"),
                    message_update(789, "/start"),
                ]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == telegram_bot.TYPE_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT
    assert store.get_task(pid, 1)["type"] == "Task"


def test_help_mentions_type():
    assert "/type" in telegram_bot.HELP_TEXT


# --- /attach (caption flow, async file handler) --------------------------------


def _attach_build(store, auth=None):
    """The build_dispatch seam wiring: the production _amain wiring
    (make_dispatch(store, auth, api=api)) against the mock transport."""
    return lambda api: telegram_bot.make_dispatch(store, auth, api=api)


def test_attach_document_upload(store):
    """A captioned document: getFile + the CDN download are both made, the
    store row has the uploaded bytes / file_name / declared mime_type, and
    the confirmation carries the task button plus the Main-menu row."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    body = b"# plan\n\nthe content"
    script = run_bot_until_stop(
        Script(
            [
                [
                    document_update(
                        801,
                        caption="/attach yask 1",
                        file_name="plan.md",
                        mime_type="text/markdown",
                        file_size=len(body),
                    )
                ]
            ],
            file_bytes=body,
        ),
        build_dispatch=_attach_build(store),
    )
    assert script.file_gets == ["DOC-1"]
    assert script.file_downloads == [f"/file/bot{BOT_TOKEN}/cdn/path/FILE-1"]
    attachments = store.list_attachments(pid, 1)
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "plan.md"
    assert attachments[0]["content_type"] == "text/markdown"
    assert attachments[0]["size"] == len(body)
    meta, data = store.get_task_attachment(pid, 1, attachments[0]["id"])
    assert data == body
    assert meta["filename"] == "plan.md"
    assert len(script.sent) == 1
    assert script.sent[0]["text"] == (
        f"Attached 'plan.md' to #1 'the bug' "
        f"({telegram_bot._human_size(len(body))})."
    )
    assert script.sent[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "#1 the bug", "callback_data": f"t:{pid}:1"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }


def test_attach_photo_upload_takes_largest_size(store):
    """A captioned photo: the largest PhotoSize's file_id is fetched; the
    store row is photo.jpg / image/jpeg."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    body = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
    script = run_bot_until_stop(
        Script([[photo_update(802, caption="/attach yask 1")]], file_bytes=body),
        build_dispatch=_attach_build(store),
    )
    # the largest PhotoSize (the last element) is the one fetched
    assert script.file_gets == ["PHOTO-L"]
    assert script.file_downloads == [f"/file/bot{BOT_TOKEN}/cdn/path/FILE-1"]
    attachments = store.list_attachments(pid, 1)
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "photo.jpg"
    assert attachments[0]["content_type"] == "image/jpeg"
    assert attachments[0]["size"] == len(body)
    assert script.sent[0]["text"] == (
        f"Attached 'photo.jpg' to #1 'the bug' "
        f"({telegram_bot._human_size(len(body))})."
    )


def test_attach_disallowed_type_no_download(store):
    """A type outside the store's allowlist is refused with a clear reply —
    before any getFile or download, and nothing is stored."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    script = run_bot_until_stop(
        Script(
            [
                [
                    document_update(
                        803,
                        caption="/attach yask 1",
                        file_name="doc.pdf",
                        mime_type="application/pdf",
                        file_size=500,
                    ),
                    document_update(
                        804,
                        caption="/attach yask 1",
                        file_name="movie.mp4",
                        mime_type="video/mp4",
                        file_size=500,
                    ),
                ]
            ],
        ),
        build_dispatch=_attach_build(store),
    )
    assert len(script.sent) == 2
    assert "application/pdf" in script.sent[0]["text"]
    assert "not allowed" in script.sent[0]["text"]
    assert "video/mp4" in script.sent[1]["text"]
    assert "not allowed" in script.sent[1]["text"]
    assert script.file_gets == []
    assert script.file_downloads == []
    assert store.list_attachments(pid, 1) == []


def test_attach_oversized_no_download(store):
    """A declared size above the 10 MB cap is refused with a clear reply —
    no getFile, no download, nothing stored."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    script = run_bot_until_stop(
        Script(
            [
                [
                    document_update(
                        805,
                        caption="/attach yask 1",
                        file_name="big.md",
                        mime_type="text/markdown",
                        file_size=11 * 1024 * 1024,
                    )
                ]
            ],
        ),
        build_dispatch=_attach_build(store),
    )
    assert len(script.sent) == 1
    assert "too large" in script.sent[0]["text"]
    assert "10 MB" in script.sent[0]["text"]
    assert script.file_gets == []
    assert script.file_downloads == []
    assert store.list_attachments(pid, 1) == []


def test_attach_caption_variants(store):
    """No caption → usage; a caption of another command → the unknown
    hint; /attach with no args → usage. None of them downloads."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    script = run_bot_until_stop(
        Script(
            [
                [document_update(806, caption=None)],
                [document_update(807, caption="/tasks")],
                [document_update(808, caption="/attach")],
            ]
        ),
        build_dispatch=_attach_build(store),
    )
    assert len(script.sent) == 3
    assert script.sent[0]["text"] == telegram_bot.ATTACH_USAGE_TEXT
    assert script.sent[1]["text"] == telegram_bot.UNKNOWN_HINT
    assert script.sent[2]["text"] == telegram_bot.ATTACH_USAGE_TEXT
    assert script.file_gets == []
    assert script.file_downloads == []
    assert store.list_attachments(pid, 1) == []


def test_attach_resolution_failures_no_download(store):
    """Unknown project / unknown task / ambiguous title: the command path's
    not-found and disambiguation texts pass through, and nothing is
    downloaded or stored."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    store.create_task(pid, "dup")
    store.create_task(pid, "dup")
    script = run_bot_until_stop(
        Script(
            [
                [document_update(809, caption="/attach nope 1")],
                [document_update(810, caption="/attach yask 999")],
                [document_update(811, caption="/attach yask dup")],
            ]
        ),
        build_dispatch=_attach_build(store),
    )
    assert len(script.sent) == 3
    assert script.sent[0]["text"] == (
        "Project 'nope' not found. Use /projects to list projects."
    )
    assert script.sent[1]["text"] == "Task #999 not found in yask."
    assert script.sent[2]["text"] == (
        "Several tasks in yask match 'dup':\n"
        "  #2 dup — Backlog\n"
        "  #3 dup — Backlog\n"
        "Use /task yask <number>."
    )
    assert script.file_gets == []
    assert script.file_downloads == []
    assert store.list_attachments(pid, 1) == []


def test_attach_requires_auth(store):
    """An unauthenticated chat gets the auth notice — no resolution, no
    download, no store row."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    store.add_telegram_user(7, "pw")
    auth = telegram_bot.Auth(store)
    script = run_bot_until_stop(
        Script(
            [[document_update(812, caption="/attach yask 1")]],
            file_bytes=b"secret",
        ),
        build_dispatch=_attach_build(store, auth),
    )
    assert script.sent[0]["text"] == telegram_bot.AUTH_REQUIRED_TEXT
    assert script.file_gets == []
    assert script.file_downloads == []
    assert store.list_attachments(pid, 1) == []


def test_attach_typed_as_text_gets_usage(store):
    """Typing /attach as a plain text message (no file) answers the usage
    text through the command table row."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    script = run_bot_until_stop(
        Script(
            [
                [message_update(813, "/attach")],
                [message_update(814, "/attach yask 1")],
            ]
        ),
        build_dispatch=_attach_build(store),
    )
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == telegram_bot.ATTACH_USAGE_TEXT
    assert script.sent[1]["text"] == telegram_bot.ATTACH_USAGE_TEXT
    assert store.list_attachments(pid, 1) == []


def test_attach_without_api_answers_error_text(store):
    """A dispatch built without a BotAPI cannot download: the attach error
    text, no getFile."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    script = run_bot_until_stop(
        Script([[document_update(815, caption="/attach yask 1")]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    assert script.sent[0]["text"] == telegram_bot.ATTACH_ERROR_TEXT
    assert script.file_gets == []
    assert script.file_downloads == []
    assert store.list_attachments(pid, 1) == []


def test_attach_download_failure_replies_error(store):
    """A failed CDN download (404 → BotAPIError) answers the attach error
    text; nothing is stored."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    # file_bytes unset → the mock CDN answers 404
    script = run_bot_until_stop(
        Script([[document_update(816, caption="/attach yask 1")]]),
        build_dispatch=_attach_build(store),
    )
    assert script.sent[0]["text"] == telegram_bot.ATTACH_ERROR_TEXT
    assert script.file_gets == ["DOC-1"]
    assert len(script.file_downloads) == 1
    assert store.list_attachments(pid, 1) == []


def test_attach_missing_metadata_falls_back_and_is_rejected(store):
    """A document with no file_name / mime_type / size: the octet-stream
    fallback is then refused by the allowlist — a clear reply, no download."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    script = run_bot_until_stop(
        Script(
            [
                [
                    file_message_update(
                        817,
                        document={"file_id": "DOC-2"},
                        caption="/attach yask 1",
                    )
                ]
            ],
        ),
        build_dispatch=_attach_build(store),
    )
    assert len(script.sent) == 1
    assert "application/octet-stream" in script.sent[0]["text"]
    assert "not allowed" in script.sent[0]["text"]
    assert script.file_gets == []
    assert script.file_downloads == []
    assert store.list_attachments(pid, 1) == []


def test_attach_missing_size_backstopped_by_store_cap(store):
    """When Telegram omits file_size the pre-check is skipped; the store's
    10 MB cap still rejects the downloaded bytes (the attach error text)."""
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "the bug")
    body = b"x" * (Store.MAX_ATTACHMENT_SIZE + 1)
    script = run_bot_until_stop(
        Script(
            [
                [
                    document_update(
                        818,
                        caption="/attach yask 1",
                        file_size=None,
                    )
                ]
            ],
            file_bytes=body,
        ),
        build_dispatch=_attach_build(store),
    )
    assert script.sent[0]["text"] == telegram_bot.ATTACH_ERROR_TEXT
    assert script.file_gets == ["DOC-1"]
    assert store.list_attachments(pid, 1) == []


def test_main_wires_file_handler_end_to_end(tmp_path):
    """The production wiring: main() passes the BotAPI into make_dispatch,
    so a captioned document uploaded to the bot's own store gets attached
    (without the api=api hand-off this answers the attach error text)."""
    conn = db.connect(tmp_path / "yask.db")
    seed = Store(conn)
    pid = seed.create_project("yask")["id"]
    seed.create_task(pid, "the bug")
    seed.add_telegram_user(7, "pw")
    conn.close()

    body = b"hello attachment"
    script = Script(
        [
            # the file handler is auth-gated: log in first
            [message_update(919, "/login pw")],
            [
                document_update(
                    920,
                    caption="/attach yask 1",
                    file_name="note.md",
                    mime_type="text/markdown",
                    file_size=len(body),
                )
            ],
        ],
        file_bytes=body,
    )
    script.stop = asyncio.Event()
    client = make_client(script.handler)
    code = telegram_bot.main(
        BOT_TOKEN, tmp_path, client=client, stop_event=script.stop
    )
    assert code == 0
    assert script.file_gets == ["DOC-1"]
    assert len(script.file_downloads) == 1
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == telegram_bot.LOGIN_OK_TEXT
    assert script.sent[1]["text"] == (
        f"Attached 'note.md' to #1 'the bug' "
        f"({telegram_bot._human_size(len(body))})."
    )
    conn = db.connect(tmp_path / "yask.db")
    try:
        store = Store(conn)
        attachments = store.list_attachments(pid, 1)
        assert [a["filename"] for a in attachments] == ["note.md"]
        assert attachments[0]["content_type"] == "text/markdown"
        meta, data = store.get_task_attachment(pid, 1, attachments[0]["id"])
        assert data == body
    finally:
        conn.close()


def test_sticker_message_still_ignored():
    """Non-media, non-text updates get no reply (as before the /attach
    file handler)."""
    script = run_bot_until_stop(
        Script(
            [
                [
                    {
                        "update_id": 819,
                        "message": {
                            "message_id": 1,
                            "chat": {"id": 7},
                            "sticker": {"file_id": "STK-1"},
                        },
                    }
                ]
            ]
        )
    )
    assert script.sent == []
    assert script.sent_files == []
    assert script.file_downloads == []


def test_help_and_command_menu_include_describe_and_attach():
    """Registry-driven: both new commands are in /help and in the
    setMyCommands payload (and in the dispatch table's gated set)."""
    assert "/describe" in telegram_bot.HELP_TEXT
    assert "/attach" in telegram_bot.HELP_TEXT
    names = [
        e["command"]
        for e in telegram_bot.build_my_commands(telegram_bot.COMMAND_REGISTRY)
    ]
    assert "/describe" in names
    assert "/attach" in names
    assert set(telegram_bot.COMMAND_TABLE) == {
        c.name for c in telegram_bot.COMMAND_REGISTRY if c.auth_gated
    }


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


# --- command registry (source of truth for /help + setMyCommands) ---------

_REGISTRY_NAMES = (
    "/start", "/help", "/login", "/whoami", "/projects", "/tasks",
    "/task", "/backlog", "/blocked", "/move", "/add", "/describe", "/type",
    "/attachment", "/attach", "/subscribe", "/unsubscribe",
)


def test_registry_covers_all_commands():
    names = [c.name for c in telegram_bot.COMMAND_REGISTRY]
    assert set(_REGISTRY_NAMES).issubset(set(names))


def test_registry_invariants():
    for command in telegram_bot.COMMAND_REGISTRY:
        assert len(command.name) <= 32
        assert len(command.description) <= 256
        assert isinstance(command.auth_gated, bool)


def test_help_lists_every_registry_command():
    for command in telegram_bot.COMMAND_REGISTRY:
        assert command.name in telegram_bot.HELP_TEXT
        assert command.description in telegram_bot.HELP_TEXT


def test_help_order_matches_registry_order():
    text = telegram_bot.HELP_TEXT
    positions = [text.index(c.name) for c in telegram_bot.COMMAND_REGISTRY]
    assert positions == sorted(positions)


def test_help_and_registry_have_same_command_set():
    # Vice-versa of test_help_lists_every_registry_command: every command
    # rendered in /help is in the registry (the inverse direction is
    # untested by the forward check). HELP_TEXT is generated from the
    # registry (render_help), so parse its command lines and compare.
    rendered = [
        line.split(" — ")[0]
        for line in telegram_bot.HELP_TEXT.splitlines()
        if line != "Commands:" and " — " in line
    ]
    assert rendered == [c.name for c in telegram_bot.COMMAND_REGISTRY]


def test_dispatch_table_covers_exactly_gated_registry_commands():
    # The dispatch table is verified against the registry at import
    # (telegram_bot._verify_dispatch_table); this test pins the same
    # invariant from the test side: COMMAND_TABLE holds exactly the
    # registry's auth_gated commands — no more, no fewer.
    gated = {c.name for c in telegram_bot.COMMAND_REGISTRY if c.auth_gated}
    assert set(telegram_bot.COMMAND_TABLE) == gated


def test_dispatch_table_error_texts_match_family_constants():
    # Each table row's error text is the per-command family constant, so
    # a store failure on a command keeps replying with its own error
    # text (the old if-chain's per-command except clauses).
    expected = {
        "/projects": telegram_bot.PROJECTS_ERROR_TEXT,
        "/tasks": telegram_bot.TASKS_ERROR_TEXT,
        "/backlog": telegram_bot.BACKLOG_ERROR_TEXT,
        "/blocked": telegram_bot.BLOCKED_ERROR_TEXT,
        "/task": telegram_bot.TASK_ERROR_TEXT,
        "/attachment": telegram_bot.ATTACHMENT_ERROR_TEXT,
        "/attach": telegram_bot.ATTACH_ERROR_TEXT,
        "/move": telegram_bot.MOVE_ERROR_TEXT,
        "/add": telegram_bot.ADD_ERROR_TEXT,
        "/describe": telegram_bot.DESCRIBE_ERROR_TEXT,
        "/type": telegram_bot.TYPE_ERROR_TEXT,
        "/subscribe": telegram_bot.SUBSCRIBE_ERROR_TEXT,
        "/unsubscribe": telegram_bot.UNSUBSCRIBE_ERROR_TEXT,
    }
    actual = {cmd: row[1] for cmd, row in telegram_bot.COMMAND_TABLE.items()}
    assert actual == expected


def test_error_family_constants_pin_published_strings():
    # Aliases cannot drift from the family constants (they are
    # assignments), but the published wording is product-facing and
    # currently unpinned: pin it.
    assert telegram_bot.READ_ERROR_TEXT == (
        "I could not read the board right now. Please try again.")
    assert telegram_bot.WRITE_ERROR_TEXT == (
        "I could not write to the board right now. Please try again.")
    for name in ("PROJECTS_ERROR_TEXT", "TASKS_ERROR_TEXT",
                 "BACKLOG_ERROR_TEXT", "BLOCKED_ERROR_TEXT",
                 "TASK_ERROR_TEXT", "ATTACHMENT_ERROR_TEXT"):
        assert getattr(telegram_bot, name) == telegram_bot.READ_ERROR_TEXT
    for name in ("MOVE_ERROR_TEXT", "ADD_ERROR_TEXT",
                 "DESCRIBE_ERROR_TEXT", "TYPE_ERROR_TEXT",
                 "ATTACH_ERROR_TEXT"):
        assert getattr(telegram_bot, name) == telegram_bot.WRITE_ERROR_TEXT


# --- command menu (setMyCommands, #66) ----------------------------------------


def test_build_my_commands_shape_and_order():
    payload = telegram_bot.build_my_commands(telegram_bot.COMMAND_REGISTRY)
    assert payload == [
        {"command": c.name, "description": c.description}
        for c in telegram_bot.COMMAND_REGISTRY
    ]
    # registry order preserved
    assert [entry["command"] for entry in payload] == [
        c.name for c in telegram_bot.COMMAND_REGISTRY
    ]
    # auth-gated commands are included (scope is presentation-only; the bot
    # still gates them — see #66 decision log). build_my_commands returns an
    # entry for every registry command, so assert the gated ones are a subset.
    assert {e["command"] for e in payload}.issuperset(
        {"/projects", "/tasks", "/task", "/move", "/add",
         "/attachment", "/subscribe", "/unsubscribe"}
    )
    assert len(payload) == len(telegram_bot.COMMAND_REGISTRY)


def test_register_my_commands_posts_scope_all_private_chats():
    script = Script([])

    async def calls(api):
        await telegram_bot.register_my_commands(api)

    bot_api_call(script, calls)
    assert len(script.command_requests) == 1
    method, body = script.command_requests[0]
    assert method == "setMyCommands"
    assert body["scope"] == {"type": "all_private_chats"}
    assert body["commands"] == telegram_bot.build_my_commands(
        telegram_bot.COMMAND_REGISTRY
    )
    # no language_code when unset
    assert "language_code" not in body


def test_register_my_commands_logs_failure_not_raises(capsys):
    script = Script([])
    api = telegram_bot.BotAPI(BOT_TOKEN)

    async def boom(api):
        async def set_my_commands(*a, **k):
            raise telegram_bot.BotAPIError("telegram unreachable")
        api.set_my_commands = set_my_commands
        await telegram_bot.register_my_commands(api)

    asyncio.run(boom(api))
    # returns cleanly (no exception propagates) and logs "not set"
    out = capsys.readouterr()
    assert "not set" in out.err
    assert script.command_requests == []


def test_startup_posts_registry_commands_via_setmycommands(tmp_path):
    # Exercise the real startup path: main() -> getMe -> register_my_commands
    # -> poll loop. The menu is posted at startup from the registry, so the
    # exact payload must match build_my_commands(COMMAND_REGISTRY).
    script = Script([[message_update(71, "/start")]])
    script.stop = asyncio.Event()
    client = make_client(script.handler)
    code = telegram_bot.main(
        BOT_TOKEN, tmp_path, client=client, stop_event=script.stop
    )
    assert code == 0
    # exactly one command-registry call, posted at startup via setMyCommands
    assert len(script.command_requests) == 1
    method, body = script.command_requests[0]
    assert method == "setMyCommands"
    assert body["scope"] == {"type": "all_private_chats"}
    assert body["commands"] == telegram_bot.build_my_commands(
        telegram_bot.COMMAND_REGISTRY
    )
    # strong exactness: registry names in order, all 17, well-formed entries
    assert [e["command"] for e in body["commands"]] == [
        c.name for c in telegram_bot.COMMAND_REGISTRY
    ]
    assert len(body["commands"]) == 17
    for entry in body["commands"]:
        assert set(entry) == {"command", "description"}


def test_startup_menu_registration_failure_does_not_break_polling(tmp_path, capsys):
    # setMyCommands fails specifically at startup; registration failure is
    # logged, not fatal, so polling still proceeds and answers /start.
    script = Script(
        [[message_update(71, "/start")]], fail_set_my_commands=True
    )
    script.stop = asyncio.Event()
    client = make_client(script.handler)
    code = telegram_bot.main(
        BOT_TOKEN, tmp_path, client=client, stop_event=script.stop
    )
    assert code == 0
    assert "command menu not set" in capsys.readouterr().err
    # polling still answered /start
    assert len(script.sent) == 1
    assert script.sent[0]["text"] == telegram_bot.START_TEXT
    # the failed call was still made / recorded (registration was attempted)
    assert script.command_requests[0][0] == "setMyCommands"


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
    """The two-row keyboard a notification message carries: the task button
    and, under it, the Main-menu row."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"#{t['number']} {title}",
                    "callback_data": f"t:{pid}:{t['number']}",
                }
            ],
            [{"text": "Main menu", "callback_data": "h"}],
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
            ],
            [{"text": "Main menu", "callback_data": "h"}],
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

    # subscribe confirmation, /start and the notification go out plain;
    # the detail view is the only Rich Message
    assert [m["chat_id"] for m in script.sent] == [7, 7, 7]
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
    # (mocked) HTTP layer as a Rich Message, with the view's own keyboard
    # (chat 7 subscribed → the toggle in its Unsubscribe state)
    expected_detail = telegram_bot.format_task_view(
        store.get_task(pid, t["number"]),
        store.get_project(pid),
        store.get_history(pid, t["number"]),
        chat_id=7,
        subscribed=True,
    )
    assert isinstance(expected_detail, telegram_bot.RichReply)
    assert len(script.sent_rich) == 1
    assert script.sent_rich[0]["chat_id"] == 7
    assert (
        script.sent_rich[0]["rich_message"]["markdown"]
        == expected_detail.markdown
    )
    assert script.sent_rich[0]["reply_markup"] == expected_detail.reply_markup


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


def test_set_my_commands_serializes_commands():
    script = Script([])

    async def calls(api):
        await api.set_my_commands(
            [{"command": "/start", "description": "main"}]
        )

    bot_api_call(script, calls)
    assert len(script.command_requests) == 1
    method, body = script.command_requests[0]
    assert method == "setMyCommands"
    assert body["commands"] == [
        {"command": "/start", "description": "main"}
    ]
    # no scope/language_code when left default
    assert "scope" not in body
    assert "language_code" not in body


def test_set_my_commands_scope_and_language():
    script = Script([])
    scope = {"type": "all_private_chats", "chat_id": 7}

    async def calls(api):
        await api.set_my_commands(
            [{"command": "/start", "description": "main"}],
            scope=scope,
            language_code="en",
        )

    bot_api_call(script, calls)
    _, body = script.command_requests[0]
    assert body["commands"] == [{"command": "/start", "description": "main"}]
    assert body["scope"] == scope
    assert body["language_code"] == "en"


def test_get_my_commands_returns_parsed_list():
    script = Script([])

    async def calls(api):
        result = await api.get_my_commands()

    bot_api_call(script, calls)
    method, body = script.command_requests[0]
    assert method == "getMyCommands"
    # empty body when no scope/language
    assert body == {}


def test_get_my_commands_return_value():
    script = Script([])
    seen = {}

    async def calls(api):
        seen["result"] = await api.get_my_commands()

    bot_api_call(script, calls)
    assert seen["result"] == [
        {"command": "/start", "description": "main"}
    ]


def test_delete_my_commands_uses_delete_endpoint():
    script = Script([])
    seen = {}

    async def calls(api):
        seen["result"] = await api.delete_my_commands()

    bot_api_call(script, calls)
    method, body = script.command_requests[0]
    # dedicated deleteMyCommands endpoint, not an empty-array setMyCommands
    assert method == "deleteMyCommands"
    assert body == {}  # empty when no scope/language
    assert seen["result"] is True


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


def test_send_rich_message_body():
    markup = {"inline_keyboard": [[{"text": "go", "callback_data": "p:1"}]]}
    script = Script([])

    async def calls(api):
        await api.send_rich_message(7, "# Title")
        await api.send_rich_message(7, "# Title", reply_markup=markup)

    bot_api_call(script, calls)
    assert script.sent_rich == [
        # no markup → no key
        {"chat_id": 7, "rich_message": {"markdown": "# Title"}},
        {
            "chat_id": 7,
            "rich_message": {"markdown": "# Title"},
            "reply_markup": markup,
        },
    ]
    # a rich send never touches the plain-send recorder
    assert script.sent == []


def test_edit_message_text_rich_payload():
    markup = {"inline_keyboard": [[{"text": "off", "callback_data": "u:1"}]]}
    script = Script([])

    async def calls(api):
        await api.edit_message_text(7, 99, rich_markdown="# Rich", reply_markup=markup)

    bot_api_call(script, calls)
    assert script.edited == [
        {
            "chat_id": 7,
            "message_id": 99,
            "rich_message": {"markdown": "# Rich"},
            "reply_markup": markup,
        }
    ]
    # a rich edit sends no ``text`` key at all (a text edit of a rich
    # message fails server-side)
    assert "text" not in script.edited[0]


def test_edit_message_text_rich_blocks_payload():
    """The raw ``rich_message`` payload: a received block tree echoed
    back verbatim (the toggle's blocks edit, Bot API 10.2+)."""
    markup = {"inline_keyboard": [[{"text": "on", "callback_data": "u:1"}]]}
    blocks = [
        {"type": "heading", "level": 1, "text": "# 4 working — Story"},
        {"type": "paragraph", "text": [{"type": "bold", "text": "State:"}, " In progress"]},
    ]
    script = Script([])

    async def calls(api):
        await api.edit_message_text(
            7, 99, rich_message={"blocks": blocks}, reply_markup=markup
        )

    bot_api_call(script, calls)
    assert script.edited == [
        {
            "chat_id": 7,
            "message_id": 99,
            "rich_message": {"blocks": blocks},
            "reply_markup": markup,
        }
    ]
    # a blocks edit sends no ``text`` key at all (a text edit of a rich
    # message fails server-side)
    assert "text" not in script.edited[0]


def test_edit_message_text_requires_exactly_one_payload():
    script = Script([])

    async def calls(api):
        with pytest.raises(ValueError):
            await api.edit_message_text(7, 99)
        with pytest.raises(ValueError):
            await api.edit_message_text(7, 99, "plain", rich_markdown="# Rich")
        with pytest.raises(ValueError):
            await api.edit_message_text(7, 99, "plain", rich_message={"blocks": []})
        with pytest.raises(ValueError):
            await api.edit_message_text(
                7, 99, rich_markdown="# Rich", rich_message={"blocks": []}
            )

    bot_api_call(script, calls)
    # the guard fires before any request is made
    assert script.edited == []


def _rich_method_handler(method, status, description):
    """A MockTransport handler failing ``/bot<token>/<method>`` with
    ``ok:false`` and the API ``error_code``; any other URL is an error, so
    the new methods must route to the right endpoint."""

    def handler(request):
        if not request.url.path.endswith(f"/{method}"):
            raise AssertionError(f"unexpected URL: {request.url.path}")
        return httpx.Response(
            status,
            json={
                "ok": False,
                "error_code": status,
                "description": description,
            },
        )

    return handler


def _expect_rich_error(call, method, status, description):
    """Run one BotAPI call (``call(api)``) against a failing mock endpoint
    named ``method`` and assert the raised ``BotAPIError`` carries the API
    ``error_code`` — callers use it to detect a rich-markdown parse failure
    (400) or an unknown method on an old local Bot API server (404) and
    fall back."""
    client = make_client(_rich_method_handler(method, status, description))
    api = telegram_bot.BotAPI(BOT_TOKEN, client=client)

    async def go():
        try:
            with pytest.raises(telegram_bot.BotAPIError) as err:
                await call(api)
        finally:
            await client.aclose()
        assert err.value.error_code == status
        assert err.value.description == description

    asyncio.run(go())


def test_send_rich_message_400_parse_error_surfaces_code():
    _expect_rich_error(
        lambda api: api.send_rich_message(7, "bad [link]("),
        "sendRichMessage",
        400,
        "can't parse rich markdown: unmatched '['",
    )


def test_send_rich_message_404_unknown_method_surfaces_code():
    # an old local telegram-bot-api server answers unknown methods with
    # ok:false, error_code 404
    _expect_rich_error(
        lambda api: api.send_rich_message(7, "# hi"),
        "sendRichMessage",
        404,
        "Not Found",
    )


def test_edit_message_text_rich_400_parse_error_surfaces_code():
    _expect_rich_error(
        lambda api: api.edit_message_text(7, 99, rich_markdown="bad [link]("),
        "editMessageText",
        400,
        "can't parse rich markdown: unmatched '['",
    )


def _send_reply_via_script(script, reply, rich=True):
    """Run ``_send_reply`` once against the script's mock transport (no
    poll loop) — the send-funnel tests below."""
    client = make_client(script.handler)
    api = telegram_bot.BotAPI(BOT_TOKEN, client=client)

    async def go():
        try:
            await telegram_bot._send_reply(api, 7, reply, rich)
        finally:
            await client.aclose()

    asyncio.run(go())


def test_send_reply_rich_happy_path():
    markup = {"inline_keyboard": [[{"text": "go", "callback_data": "p:1"}]]}
    script = Script([])
    _send_reply_via_script(
        script, telegram_bot.RichReply("# Title", reply_markup=markup)
    )
    # the first leg is the Rich Message: markdown payload + threaded keyboard
    assert script.sent_rich == [
        {
            "chat_id": 7,
            "rich_message": {"markdown": "# Title"},
            "reply_markup": markup,
        }
    ]
    # no double-send: the plain-send recorder stays empty
    assert script.sent == []


def test_send_reply_rich_happy_path_no_markup():
    script = Script([])
    _send_reply_via_script(script, telegram_bot.RichReply("# Title"))
    # no markup → no key
    assert script.sent_rich == [
        {"chat_id": 7, "rich_message": {"markdown": "# Title"}}
    ]
    assert script.sent == []


def test_send_reply_rich_400_falls_back_to_html():
    markup = {"inline_keyboard": [[{"text": "go", "callback_data": "p:1"}]]}
    markdown = "**bold** and <script>alert(1)</script>"
    script = Script([], fail_rich_once=(400, "can't parse rich markdown"))
    _send_reply_via_script(script, telegram_bot.RichReply(markdown, markup))
    # the rich leg fired (and failed) exactly once ...
    assert script.sent_rich == [
        {
            "chat_id": 7,
            "rich_message": {"markdown": markdown},
            "reply_markup": markup,
        }
    ]
    # ... and the HTML leg replaced it — two sends total, no double-send
    assert len(script.sent) == 1
    body = script.sent[0]
    assert body["chat_id"] == 7
    assert body["parse_mode"] == "HTML"
    assert body["reply_markup"] == markup
    # the markdown was converted, not raw-sent: real escaping
    assert body["text"] == telegram_bot.markdown_to_html(markdown)
    assert "<strong>bold</strong>" in body["text"]
    assert "&lt;script&gt;" in body["text"]
    assert "<script>" not in body["text"]


def test_send_reply_rich_404_then_html_400_falls_back_to_plain():
    markup = {"inline_keyboard": [[{"text": "go", "callback_data": "p:1"}]]}
    markdown = "**bold**"
    script = Script(
        [],
        fail_rich_once=(404, "Not Found"),
        fail_send_once=(400, "can't parse HTML"),
    )
    _send_reply_via_script(script, telegram_bot.RichReply(markdown, markup))
    # rich leg 404 (old local Bot API server), then HTML leg 400
    assert len(script.sent_rich) == 1
    assert len(script.sent) == 2
    html_leg, plain_leg = script.sent
    assert html_leg["parse_mode"] == "HTML"
    assert html_leg["text"] == telegram_bot.markdown_to_html(markdown)
    assert html_leg["reply_markup"] == markup
    # the plain leg is today's behavior: the markdown verbatim, no
    # parse_mode key — and the only successful message, keyboard threaded
    assert plain_leg == {
        "chat_id": 7,
        "text": markdown,
        "reply_markup": markup,
    }


def test_send_reply_rich_kill_switch_off():
    markup = {"inline_keyboard": [[{"text": "go", "callback_data": "p:1"}]]}
    markdown = "**bold**"
    script = Script([], fail_rich_once=(400, "should never be called"))
    _send_reply_via_script(
        script, telegram_bot.RichReply(markdown, markup), rich=False
    )
    # no sendRichMessage call at all — the first leg is the HTML leg
    assert script.sent_rich == []
    assert len(script.sent) == 1
    body = script.sent[0]
    assert body["parse_mode"] == "HTML"
    assert body["text"] == telegram_bot.markdown_to_html(markdown)
    assert body["reply_markup"] == markup


def test_run_bot_rich_reply_kill_switch_off():
    markup = {"inline_keyboard": [[{"text": "view", "callback_data": "t:1:4"}]]}

    def dispatch(text, chat_id=None, file=None):
        return telegram_bot.RichReply("**hi**", reply_markup=markup)

    script = run_bot_until_stop(
        Script([[message_update(401, "hello")]]),
        dispatch=dispatch,
        rich=False,
    )
    # end-to-end: the kill switch is threaded through run_bot, the rich
    # leg is never attempted, the HTML leg carries the converted markdown
    assert script.sent_rich == []
    assert script.sent == [
        {
            "chat_id": 7,
            "text": "<strong>hi</strong>",
            "reply_markup": markup,
            "parse_mode": "HTML",
        }
    ]


def test_send_reply_rich_oversized_payload_retruncated_on_fallback():
    markdown = "a" * 5000
    script = Script([], fail_rich_once=(400, "too long"))
    _send_reply_via_script(script, telegram_bot.RichReply(markdown))
    # the rich leg got the full payload (well under RICH_MESSAGE_MAX)
    assert script.sent_rich[0]["rich_message"]["markdown"] == markdown
    # the fallback leg carries the re-truncated text with the note
    assert len(script.sent) == 1
    text = script.sent[0]["text"]
    assert len(text) <= telegram_bot.REGULAR_TEXT_MAX
    assert text.endswith("… (truncated, 5000 chars total)")
    # and the plain leg carries the same truncated markdown, verbatim
    script2 = Script(
        [],
        fail_rich_once=(404, "Not Found"),
        fail_send_once=(400, "bad html"),
    )
    _send_reply_via_script(script2, telegram_bot.RichReply(markdown))
    assert script2.sent[0]["text"] == text
    assert script2.sent[1] == {"chat_id": 7, "text": text}


def test_rich_enabled_env(monkeypatch):
    monkeypatch.delenv("YASK_TELEGRAM_RICH", raising=False)
    assert telegram_bot._rich_enabled() is True
    monkeypatch.setenv("YASK_TELEGRAM_RICH", "0")
    assert telegram_bot._rich_enabled() is False
    monkeypatch.setenv("YASK_TELEGRAM_RICH", "1")
    assert telegram_bot._rich_enabled() is True


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
                lambda text, chat_id=None, file=None: telegram_bot.reply_for(text),
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
    # with the p:, t:, a:, s:/u: and m:/c:/x: handlers: a well-formed
    # ``p:`` payload is handled (a fresh store has no projects → the
    # not-found reply), an unrecognised payload is still unhandled (the
    # out-of-date toast answers it), and a valid ``t:`` payload is
    # handled — the callback's message carries a chat, so the detail reply
    # is a KeyboardReply with the task view's own buttons (no attachments
    # here → just the state rows and the toggle).
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
    # answered without a toast; the detail view goes out as a new
    # Rich Message
    assert script.answered == [{"callback_query_id": "cbq-302"}]
    assert len(script.sent_rich) == 1
    sent = script.sent_rich[0]
    assert sent["chat_id"] == 11
    markdown = sent["rich_message"]["markdown"]
    assert markdown.startswith(f"# {t['number']} working — Story\n")
    assert "**State:** In progress" in markdown
    assert "**Estimate:** 3" in markdown
    # the detail carries its own keyboard: no attachments on this task →
    # the state rows (the task is In progress: the other five workflow
    # states, 3 per row) plus the subscribe toggle, reflecting chat 11's
    # subscription state
    n = t["number"]
    assert sent["reply_markup"] == {
        "inline_keyboard": [
            [
                {"text": "Backlog", "callback_data": f"m:{pid}:{n}:0"},
                {"text": "Todo", "callback_data": f"m:{pid}:{n}:1"},
                {"text": "Planning", "callback_data": f"m:{pid}:{n}:2"},
            ],
            [
                {"text": "Review", "callback_data": f"m:{pid}:{n}:4"},
                {"text": "Done", "callback_data": f"m:{pid}:{n}:5"},
            ],
            [{"text": "Subscribe", "callback_data": f"s:{pid}"}],
            [{"text": "Main menu", "callback_data": "h"}],
        ]
    }
    assert script.sent == []
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


def test_callback_dispatch_attachment_renders_inline(store):
    """An a: press on a small markdown attachment replies with a
    RichReply (pins the a: entry point at the dispatch level)."""
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
    assert isinstance(reply, telegram_bot.RichReply)
    assert reply.reply_markup is None
    assert reply.markdown == expected_rich_markdown(
        t["number"], "working", "plan.md", d["plan_bytes"].decode()
    )


def test_callback_dispatch_attachment_round_trip(store):
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    # markdown attachment → Rich Message
    script = run_bot_until_stop(
        Script(
            [[callback_update(292, f"a:{pid}:{t['number']}:{d['plan']['id']}", chat_id=13)]]
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert script.answered == [{"callback_query_id": "cbq-292"}]
    assert script.sent_files == []
    assert script.sent == []
    assert len(script.sent_rich) == 1
    assert script.sent_rich[0]["chat_id"] == 13
    assert (
        script.sent_rich[0]["rich_message"]["markdown"]
        == expected_rich_markdown(
            t["number"], "working", "plan.md", d["plan_bytes"].decode()
        )
    )
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


def test_callback_a_large_markdown_sends_document(store):
    """An a: press on a >=16 KB markdown attachment replies with a file."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    big = b"# Big\n" + b"y" * (17 * 1024)  # over the inline threshold
    a = store.add_attachment(pid, t["number"], "big.md", "text/markdown", big)
    dispatch = telegram_bot.make_callback_dispatch(store)
    action = dispatch(
        callback_update(298, f"a:{pid}:{t['number']}:{a['id']}")["callback_query"]
    )
    assert action is not None
    assert action.answer_text is None
    assert action.edit is None
    reply = action.reply
    assert isinstance(reply, telegram_bot.FileReply)
    assert reply.filename == "big.md"
    assert reply.data == big
    assert reply.content_type == "text/markdown"
    assert reply.caption == f"#{t['number']} working — big.md"


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


def _detail_callback(update_id, data, chat_id, detail, rich=None):
    """A callback_update whose original message is the /task detail Rich
    Message — the shape Telegram sends when a button on the detail
    message is pressed: the ``rich_message`` field (the received
    RichMessage is a parsed block tree, not markdown) and no ``text``
    (empty on rich messages). ``rich`` overrides the (empty) block tree
    the original carries — the toggle's tests echo it back."""
    update = callback_update(update_id, data, chat_id=chat_id)
    message = update["callback_query"]["message"]
    message["rich_message"] = {"blocks": []} if rich is None else rich
    del message["text"]
    message["reply_markup"] = detail["reply_markup"]
    return update


def _detail_plain_callback(update_id, data, chat_id, detail):
    """A callback_update whose original message is the /task detail as a
    *plain* message (the kill switch off, or the send chain degraded the
    rich view to plain text): a ``text`` field plus the detail's
    keyboard."""
    update = callback_update(update_id, data, chat_id=chat_id)
    update["callback_query"]["message"]["text"] = detail["rich_message"]["markdown"]
    update["callback_query"]["message"]["reply_markup"] = detail["reply_markup"]
    return update


def test_callback_dispatch_subscribe_toggle(store):
    """An s: press on a *plain* original re-renders in place (the
    toggle's in-place echo needs the original's ``text``)."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    chat_id = 21
    # the real /task detail (Rich Message + keyboard) as the original message
    first = run_bot_until_stop(
        Script([[message_update(298, f"/task yask {t['number']}", chat_id=chat_id)]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    detail = first.sent_rich[0]
    # a plain-shaped original (degraded view): text + the detail keyboard
    update = _detail_plain_callback(299, f"s:{pid}", chat_id, detail)
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
    # a plain original keeps the plain text edit
    assert "rich_message" not in e
    assert e["text"] == detail["rich_message"]["markdown"]
    rows = e["reply_markup"]["inline_keyboard"]
    assert rows[:2] == detail["reply_markup"]["inline_keyboard"][:2]
    assert rows[-2] == [{"text": "Unsubscribe", "callback_data": f"u:{pid}"}]
    # the Main-menu row survives the in-place toggle flip
    assert rows[-1] == [{"text": "Main menu", "callback_data": "h"}]


def test_callback_dispatch_subscribe_toggle_rich_view(store):
    """An s: press on a *rich* original flips the toggle in place: the
    original's block tree is echoed back through the rich
    editMessageText payload (Bot API 10.2+ ``blocks`` input) with the
    flipped keyboard — the message text is unchanged, nothing stacked."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    chat_id = 27
    # the real /task detail (Rich Message + keyboard) as the original message
    first = run_bot_until_stop(
        Script([[message_update(310, f"/task yask {t['number']}", chat_id=chat_id)]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    detail = first.sent_rich[0]
    # a small realistic received block tree: one heading with a plain
    # string text, one paragraph with an entity-array text
    tree = [
        {"type": "heading", "level": 1, "text": "# 4 working — Story"},
        {"type": "paragraph", "text": [{"type": "bold", "text": "State:"}, " In progress"]},
    ]
    update = _detail_callback(311, f"s:{pid}", chat_id, detail, rich={"blocks": tree})
    script = run_bot_until_stop(
        Script([[update]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the store row is created and the press is toasted
    assert [s["project_id"] for s in store.list_subscriptions(chat_id)] == [pid]
    assert script.answered == [
        {"callback_query_id": "cbq-311", "text": "Subscribed to yask"}
    ]
    # exactly one in-place edit: the rich payload echoes the original's
    # block tree byte-for-byte (no ``text`` key — a text edit of a rich
    # message fails server-side), the attachment rows are preserved, the
    # toggle row flips to Unsubscribe, the Main-menu row stays last
    assert len(script.edited) == 1
    e = script.edited[0]
    assert e["chat_id"] == chat_id
    assert e["message_id"] == 1
    assert "text" not in e
    assert e["rich_message"] == {"blocks": tree}
    rows = e["reply_markup"]["inline_keyboard"]
    assert rows[:2] == detail["reply_markup"]["inline_keyboard"][:2]
    assert rows[-2] == [{"text": "Unsubscribe", "callback_data": f"u:{pid}"}]
    assert rows[-1] == [{"text": "Main menu", "callback_data": "h"}]
    # nothing is stacked on top of the rich view
    assert script.sent == []
    assert script.sent_rich == []


def test_callback_dispatch_unsubscribe_toggle_rich_view(store):
    """A u: press on a *rich* original flips the toggle back in place:
    the store row is removed, the block tree is echoed, the button flips
    to Subscribe."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    chat_id = 28
    store.subscribe_project(chat_id, pid)
    first = run_bot_until_stop(
        Script([[message_update(312, f"/task yask {t['number']}", chat_id=chat_id)]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    detail = first.sent_rich[0]
    # the subscribed detail shows Unsubscribe before the Main-menu row
    assert detail["reply_markup"]["inline_keyboard"][-2] == [
        {"text": "Unsubscribe", "callback_data": f"u:{pid}"}
    ]
    tree = [{"type": "paragraph", "text": "The task at hand."}]
    update = _detail_callback(313, f"u:{pid}", chat_id, detail, rich={"blocks": tree})
    script = run_bot_until_stop(
        Script([[update]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the store row is removed and the press is toasted
    assert [s["project_id"] for s in store.list_subscriptions(chat_id)] == []
    assert script.answered == [
        {"callback_query_id": "cbq-313", "text": "Unsubscribed from yask"}
    ]
    assert len(script.edited) == 1
    e = script.edited[0]
    assert e["chat_id"] == chat_id
    assert e["message_id"] == 1
    assert "text" not in e
    assert e["rich_message"] == {"blocks": tree}
    rows = e["reply_markup"]["inline_keyboard"]
    assert rows[:2] == detail["reply_markup"]["inline_keyboard"][:2]
    assert rows[-2] == [{"text": "Subscribe", "callback_data": f"s:{pid}"}]
    assert rows[-1] == [{"text": "Main menu", "callback_data": "h"}]
    assert script.sent == []
    assert script.sent_rich == []


def test_callback_dispatch_toggle_rich_edit_failure_toasts_only(store):
    """A 404 on the blocks edit (a 10.1 local server without the blocks
    input; a 400 rejection takes the identical path) skips the in-place
    edit: toast only — the store change still applies, no fresh send (the
    stale original stays in the chat, the worst case equals the pre-#106
    behavior), and the loop survives."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    chat_id = 29
    first = run_bot_until_stop(
        Script([[message_update(314, f"/task yask {t['number']}", chat_id=chat_id)]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    detail = first.sent_rich[0]
    tree = [
        {"type": "heading", "level": 1, "text": "# 4 working — Story"},
        {"type": "paragraph", "text": [{"type": "bold", "text": "State:"}, " In progress"]},
    ]
    update = _detail_callback(315, f"s:{pid}", chat_id, detail, rich={"blocks": tree})
    script = run_bot_until_stop(
        Script(
            [[update, message_update(316, "/start")]],
            fail_edit_once=(404, "Not Found"),
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the store change applies and the press is toasted (the toast is
    # unaffected by the blocks-edit failure)
    assert [s["project_id"] for s in store.list_subscriptions(chat_id)] == [pid]
    assert script.answered == [
        {"callback_query_id": "cbq-315", "text": "Subscribed to yask"}
    ]
    # the blocks edit was attempted with the echo payload ...
    assert len(script.edited) == 1
    e = script.edited[0]
    assert e["chat_id"] == chat_id
    assert e["message_id"] == 1
    assert "text" not in e
    assert e["rich_message"] == {"blocks": tree}
    # ... failed, and no fresh message was sent (toast-only degradation):
    # the only plain send is the /start reply (to its own chat), proving
    # the loop survived
    assert script.sent_rich == []
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == telegram_bot.START_TEXT
    assert script.offsets == [None, 317]


def test_callback_dispatch_unsubscribe_toggle(store):
    """A u: press on a *plain* original re-renders in place (the
    toggle's in-place echo needs the original's ``text``)."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    chat_id = 22
    store.subscribe_project(chat_id, pid)
    first = run_bot_until_stop(
        Script([[message_update(300, f"/task yask {t['number']}", chat_id=chat_id)]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    detail = first.sent_rich[0]
    # the detail shows Unsubscribe for the subscribed chat, followed by
    # the Main-menu row
    assert detail["reply_markup"]["inline_keyboard"][-2] == [
        {"text": "Unsubscribe", "callback_data": f"u:{pid}"}
    ]
    assert detail["reply_markup"]["inline_keyboard"][-1] == [
        {"text": "Main menu", "callback_data": "h"}
    ]
    # a plain-shaped original (degraded view): text + the detail keyboard
    update = _detail_plain_callback(301, f"u:{pid}", chat_id, detail)
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
    # a plain original keeps the plain text edit
    assert "rich_message" not in e
    assert e["text"] == detail["rich_message"]["markdown"]
    rows = e["reply_markup"]["inline_keyboard"]
    assert rows[:2] == detail["reply_markup"]["inline_keyboard"][:2]
    assert rows[-2] == [{"text": "Subscribe", "callback_data": f"s:{pid}"}]
    # the Main-menu row survives the in-place toggle flip
    assert rows[-1] == [{"text": "Main menu", "callback_data": "h"}]


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


# --- m:/c:/x: payload (the /task view's state buttons) ------------------------

DONE_IDX = 5  # 'Done''s index in db.WORKFLOW_STATES


def test_callback_m_single_move_edits_detail_in_place(store):
    """An m: press on a prerequisite-free task moves it and re-renders the
    rich detail in place with the rich editMessageText payload (no
    message stacked)."""
    d = seed_task_view(store)
    pid, n = d["pid"], d["p2"]["number"]  # 'prereq two': In progress, no prereqs
    chat_id = 31
    first = run_bot_until_stop(
        Script([[message_update(721, f"/task yask {n}", chat_id=chat_id)]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    detail = first.sent_rich[0]
    update = _detail_callback(722, f"m:{pid}:{n}:{DONE_IDX}", chat_id, detail)
    script = run_bot_until_stop(
        Script([[update]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert store.get_task(pid, n)["state"] == "Done"
    assert script.answered == [
        {"callback_query_id": "cbq-722", "text": f"Moved #{n} to Done."}
    ]
    assert script.sent == []
    assert len(script.edited) == 1
    e = script.edited[0]
    assert e["chat_id"] == chat_id
    assert e["message_id"] == 1
    # a rich original gets the rich edit payload (a text edit of a rich
    # message 400s server-side)
    assert "text" not in e
    assert "**State:** Done" in e["rich_message"]["markdown"]
    # the fresh keyboard: the new current state (Done) is excluded
    labels = [
        b["text"]
        for row in e["reply_markup"]["inline_keyboard"]
        for b in row
    ]
    assert "Done" not in labels
    assert "Backlog" in labels and "Subscribe" in labels


def test_callback_m_cascade_shows_confirm_keyboard(store):
    """An m: press that would pull prerequisites edits the rich message to
    the confirm keyboard (rich payload) and writes nothing."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]  # 'working' — has two prerequisites
    n = t["number"]
    chat_id = 32
    first = run_bot_until_stop(
        Script([[message_update(723, f"/task yask {n}", chat_id=chat_id)]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    detail = first.sent_rich[0]
    update = _detail_callback(724, f"m:{pid}:{n}:{DONE_IDX}", chat_id, detail)
    script = run_bot_until_stop(
        Script([[update]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # nothing is written; the message is edited to the confirm prompt
    assert store.get_task(pid, n)["state"] == "In progress"
    assert script.answered == [{"callback_query_id": "cbq-724"}]
    assert script.sent == []
    assert len(script.edited) == 1
    e = script.edited[0]
    # the confirm prompt is plain text, but a rich original needs the
    # rich payload (a text edit of a rich message 400s server-side)
    assert "text" not in e
    assert e["rich_message"]["markdown"] == (
        f"Move #{n} to Done?\n"
        "This also moves its prerequisites that have not reached this stage:\n"
        f"  #{d['p1']['number']} prereq one — Review\n"
        f"  #{d['p2']['number']} prereq two — In progress"
    )
    assert e["reply_markup"] == {
        "inline_keyboard": [
            [
                {
                    "text": "Move all",
                    "callback_data": f"c:{pid}:{n}:{DONE_IDX}",
                }
            ],
            [
                {
                    "text": "Cancel",
                    "callback_data": f"x:{pid}:{n}",
                }
            ],
        ]
    }


def test_callback_c_confirms_cascade_and_renders_detail(store):
    """The full button flow: m: → confirm keyboard, c: → the cascade is
    applied and the detail re-renders in place."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    n = t["number"]
    chat_id = 33
    first = run_bot_until_stop(
        Script([[message_update(725, f"/task yask {n}", chat_id=chat_id)]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    detail = first.sent_rich[0]
    script = run_bot_until_stop(
        Script(
            [
                [_detail_callback(726, f"m:{pid}:{n}:{DONE_IDX}", chat_id, detail)],
                [_detail_callback(727, f"c:{pid}:{n}:{DONE_IDX}", chat_id, detail)],
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # the cascade was applied to all three tasks
    assert store.get_task(pid, n)["state"] == "Done"
    assert store.get_task(pid, d["p1"]["number"])["state"] == "Done"
    assert store.get_task(pid, d["p2"]["number"])["state"] == "Done"
    assert script.answered == [
        {"callback_query_id": "cbq-726"},
        {"callback_query_id": "cbq-727", "text": "Moved 3 tasks to Done."},
    ]
    assert script.sent == []
    assert len(script.edited) == 2
    # both edits ride the rich payload (the original is a rich view)
    assert "text" not in script.edited[0]
    assert script.edited[0]["rich_message"]["markdown"].startswith(
        f"Move #{n} to Done?"
    )
    # the second edit is the fresh detail view of the moved task
    e = script.edited[1]
    assert e["chat_id"] == chat_id
    assert "text" not in e
    assert "**State:** Done" in e["rich_message"]["markdown"]
    labels = [
        b["text"]
        for row in e["reply_markup"]["inline_keyboard"]
        for b in row
    ]
    assert "Done" not in labels
    assert "plan.md" in labels  # the attachment rows are back


def test_callback_x_cancels_and_renders_detail(store):
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    n = t["number"]
    chat_id = 34
    first = run_bot_until_stop(
        Script([[message_update(728, f"/task yask {n}", chat_id=chat_id)]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    detail = first.sent_rich[0]
    update = _detail_callback(729, f"x:{pid}:{n}", chat_id, detail)
    script = run_bot_until_stop(
        Script([[update]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    # nothing moved; the rich message is re-rendered to the plain detail
    # view with the rich payload
    assert store.get_task(pid, n)["state"] == "In progress"
    assert script.answered == [
        {"callback_query_id": "cbq-729", "text": "Cancelled."}
    ]
    assert script.sent == []
    e = script.edited[0]
    assert "text" not in e
    assert e["rich_message"]["markdown"].startswith(f"# {n} working — Story")
    assert "**State:** In progress" in e["rich_message"]["markdown"]
    labels = [
        b["text"]
        for row in e["reply_markup"]["inline_keyboard"]
        for b in row
    ]
    assert "Done" in labels  # the state buttons are back


def test_callback_m_rich_edit_400_degrades_to_fresh_message(store):
    """A 400 on the rich edit skips the in-place edit and re-sends the
    re-rendered view as a fresh rich message; the loop survives."""
    d = seed_task_view(store)
    pid, n = d["pid"], d["p2"]["number"]  # 'prereq two': In progress, no prereqs
    chat_id = 36
    first = run_bot_until_stop(
        Script([[message_update(740, f"/task yask {n}", chat_id=chat_id)]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    detail = first.sent_rich[0]
    update = _detail_callback(741, f"m:{pid}:{n}:{DONE_IDX}", chat_id, detail)
    script = run_bot_until_stop(
        Script(
            [[update, message_update(742, "/start")]],
            fail_edit_once=(400, "can't parse rich message"),
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert store.get_task(pid, n)["state"] == "Done"
    # the move was applied and toasted (the toast is unaffected by the
    # rich-edit failure)
    assert script.answered == [
        {"callback_query_id": "cbq-741", "text": f"Moved #{n} to Done."}
    ]
    # the rich edit was attempted with the rich payload ...
    assert len(script.edited) == 1
    e = script.edited[0]
    assert e["chat_id"] == chat_id
    assert e["message_id"] == 1
    assert "text" not in e
    assert "**State:** Done" in e["rich_message"]["markdown"]
    # ... failed, and the re-render went out as a fresh rich message
    # (the user sees the updated view instead of the stale original)
    assert len(script.sent_rich) == 1
    sent = script.sent_rich[0]
    assert sent["chat_id"] == chat_id
    assert sent["rich_message"]["markdown"] == e["rich_message"]["markdown"]
    assert sent["reply_markup"] == e["reply_markup"]
    # the rich leg of the fresh send succeeded: no plain fallback, and the
    # only plain send is the /start reply (to its own chat) proving the
    # loop survived
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == telegram_bot.START_TEXT
    assert script.offsets == [None, 743]


def test_callback_m_rich_edit_404_degrades_through_chain(store):
    """A 404 on the rich edit degrades the fresh send through the
    rich → HTML chain (old local Bot API server); the loop survives."""
    d = seed_task_view(store)
    pid, n = d["pid"], d["p2"]["number"]
    chat_id = 37
    first = run_bot_until_stop(
        Script([[message_update(743, f"/task yask {n}", chat_id=chat_id)]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    detail = first.sent_rich[0]
    update = _detail_callback(744, f"m:{pid}:{n}:{DONE_IDX}", chat_id, detail)
    script = run_bot_until_stop(
        Script(
            [[update, message_update(745, "/start")]],
            fail_edit_once=(404, "Not Found"),
            fail_rich_once=(404, "Not Found"),
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert store.get_task(pid, n)["state"] == "Done"
    assert script.answered == [
        {"callback_query_id": "cbq-744", "text": f"Moved #{n} to Done."}
    ]
    # the rich edit was attempted (and 404'd) ...
    assert len(script.edited) == 1
    assert "text" not in script.edited[0]
    assert "**State:** Done" in script.edited[0]["rich_message"]["markdown"]
    # ... the fresh send's rich leg also failed and the HTML leg landed
    assert len(script.sent_rich) == 1  # the failed rich attempt
    assert len(script.sent) == 2
    html_leg, start_leg = script.sent
    assert html_leg["chat_id"] == chat_id
    assert html_leg["parse_mode"] == "HTML"
    # the detail view is far under REGULAR_TEXT_MAX: no re-truncation
    assert html_leg["text"] == telegram_bot.markdown_to_html(
        script.edited[0]["rich_message"]["markdown"]
    )
    assert html_leg["reply_markup"] == script.edited[0]["reply_markup"]
    # the /start reply (to its own chat) proves the loop survived
    assert start_leg["chat_id"] == 7
    assert start_leg["text"] == telegram_bot.START_TEXT
    assert script.offsets == [None, 746]


def test_callback_m_plain_view_edit_unchanged(store):
    """An m: press on a *plain* original keeps the plain text edit —
    the pre-rich behavior, byte-identical."""
    d = seed_task_view(store)
    pid, n = d["pid"], d["p2"]["number"]
    chat_id = 38
    first = run_bot_until_stop(
        Script([[message_update(746, f"/task yask {n}", chat_id=chat_id)]]),
        dispatch=telegram_bot.make_dispatch(store),
    )
    detail = first.sent_rich[0]
    # a plain-shaped original: has text, no rich_message
    update = _detail_plain_callback(747, f"m:{pid}:{n}:{DONE_IDX}", chat_id, detail)
    script = run_bot_until_stop(
        Script([[update]]),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert store.get_task(pid, n)["state"] == "Done"
    assert script.answered == [
        {"callback_query_id": "cbq-747", "text": f"Moved #{n} to Done."}
    ]
    assert script.sent == []
    assert len(script.edited) == 1
    e = script.edited[0]
    assert "rich_message" not in e
    assert "**State:** Done" in e["text"]
    labels = [
        b["text"]
        for row in e["reply_markup"]["inline_keyboard"]
        for b in row
    ]
    assert "Done" not in labels
    assert "Backlog" in labels and "Subscribe" in labels


def test_callback_m_already_in_state_toasts_only(store):
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    n = t["number"]
    dispatch = telegram_bot.make_callback_dispatch(store)
    # 'working' is In progress (index 3): pressing its own state toasts and
    # changes nothing
    action = dispatch(
        callback_update(731, f"m:{pid}:{n}:3", chat_id=35)["callback_query"]
    )
    assert action is not None
    assert action.answer_text == "Already in In progress."
    assert action.reply is None
    assert action.edit is None
    assert store.get_task(pid, n)["state"] == "In progress"


def test_callback_c_stale_confirm_toasts_already(store):
    """Someone moved the task in the meantime: the stale c: press toasts
    'Already in …' and writes nothing."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    n = t["number"]
    store.move_task(pid, n, "Done", confirm=True)
    dispatch = telegram_bot.make_callback_dispatch(store)
    action = dispatch(
        callback_update(732, f"c:{pid}:{n}:{DONE_IDX}", chat_id=36)["callback_query"]
    )
    assert action is not None
    assert action.answer_text == "Already in Done."
    assert action.reply is None
    assert action.edit is None


def test_callback_m_archived_task_is_out_of_date(store):
    """Archived details never carry state buttons (or confirms): a stale
    m:/c: press on an archived task is unhandled → the out-of-date toast."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    store.archive_task(pid, t["number"], confirm=True)
    dispatch = telegram_bot.make_callback_dispatch(store)
    for payload in (
        f"m:{pid}:{t['number']}:{DONE_IDX}",
        f"c:{pid}:{t['number']}:{DONE_IDX}",
    ):
        assert (
            dispatch(callback_update(733, payload)["callback_query"]) is None
        ), payload


def test_callback_m_unknown_task_and_project(store):
    pid = store.create_project("alpha")["id"]
    dispatch = telegram_bot.make_callback_dispatch(store)
    action = dispatch(callback_update(734, f"m:{pid}:42:1")["callback_query"])
    assert action is not None
    assert action.reply == "Task #42 not found in alpha."
    action = dispatch(callback_update(735, "m:999:1:1")["callback_query"])
    assert action is not None
    assert action.reply == (
        "Project '999' not found. Use /projects to list projects."
    )
    # x: has the same resolution
    action = dispatch(callback_update(736, f"x:{pid}:42")["callback_query"])
    assert action is not None
    assert action.reply == "Task #42 not found in alpha."


def test_callback_m_store_failure_replies_and_recovers(store, monkeypatch):
    """A store failure on an m: press replies with the error text, no crash."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")

    def boom(project_id, number, to_state):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "plan_move", boom)
    script = run_bot_until_stop(
        Script(
            [
                [
                    callback_update(737, f"m:{pid}:{t['number']}:4", chat_id=11),
                    message_update(738, "/start"),
                ]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert script.answered == [{"callback_query_id": "cbq-737"}]
    assert len(script.sent) == 2
    assert script.sent[0]["chat_id"] == 11
    assert script.sent[0]["text"] == telegram_bot.MOVE_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT
    assert script.edited == []
    # the failed move leaves the task in place
    assert store.get_task(pid, t["number"])["state"] == "Backlog"


def test_callback_c_store_failure_replies_and_recovers(store, monkeypatch):
    """A store failure on a c: press replies with the error text, no crash."""
    pid = store.create_project("yask")["id"]
    t = store.create_task(pid, "working")
    store.move_task(pid, t["number"], "Todo", confirm=True)

    def boom(project_id, number, to_state, confirm=False, **kw):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store, "move_task", boom)
    script = run_bot_until_stop(
        Script(
            [
                [
                    callback_update(739, f"c:{pid}:{t['number']}:4", chat_id=11),
                    message_update(740, "/start"),
                ]
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store),
        callback_dispatch=telegram_bot.make_callback_dispatch(store),
    )
    assert script.answered == [{"callback_query_id": "cbq-739"}]
    assert len(script.sent) == 2
    assert script.sent[0]["text"] == telegram_bot.MOVE_ERROR_TEXT
    assert script.sent[1]["text"] == telegram_bot.START_TEXT
    assert script.edited == []
    assert store.get_task(pid, t["number"])["state"] == "Todo"


@pytest.mark.parametrize(
    "payload",
    [
        "t:1", "t:1:4:9", "t:x:4", "t:", "p:", "p:abc", "stale:payload",
        "a:1", "a:1:2", "a:1:2:3:4:5", "a:x:1:2", "a:1:x:2", "a:1:2:x",
        "s:", "s:abc", "s:1:2", "u:", "u:abc", "u:1:2",
        "m:", "m:1", "m:1:2", "m:1:2:3:4", "m:x:2:3", "m:1:x:3",
        "m:1:2:x", "m:1:2:99", "c:", "c:1:2", "c:1:2:99",
        "x:", "x:1", "x:1:2:3", "x:abc:1", "x:1:abc",
        "h:", "h:x", "h:1", "h:p:1", "h::", "H",
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


# --- authentication (/login, /whoami, the board gate) --------------------------


def test_auth_none_chat_is_never_authenticated(store):
    store.add_telegram_user(7, "pw")
    auth = telegram_bot.Auth(store)
    assert auth.is_authenticated(None) is False
    assert auth.is_authenticated(7) is False  # permitted, but not logged in
    assert auth.authenticate(None, "pw") is False
    assert auth.is_authenticated(None) is False


def test_login_success(store):
    store.add_telegram_user(7, "pw123")
    auth = telegram_bot.Auth(store)
    script = run_bot_until_stop(
        Script([[message_update(601, "/login pw123")]]),
        dispatch=telegram_bot.make_dispatch(store, auth),
    )
    assert len(script.sent) == 1
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == telegram_bot.LOGIN_OK_TEXT
    assert auth.is_authenticated(7)


def test_login_wrong_password_fails(store):
    store.add_telegram_user(7, "pw123")
    auth = telegram_bot.Auth(store)
    script = run_bot_until_stop(
        Script([[message_update(602, "/login wrong")]]),
        dispatch=telegram_bot.make_dispatch(store, auth),
    )
    assert script.sent[0]["text"] == telegram_bot.LOGIN_FAIL_TEXT
    assert not auth.is_authenticated(7)


def test_login_unlisted_chat_fails_like_wrong_password(store):
    """An unknown chat gets the exact same failure text as a wrong password
    — the bot must not reveal which chat ids exist in the allowlist."""
    store.add_telegram_user(7, "pw")
    auth = telegram_bot.Auth(store)
    script = run_bot_until_stop(
        Script(
            [
                # chat 8 is not in the allowlist at all — and "pw" is chat
                # 7's real password, so this is the unknown-chat path
                [message_update(603, "/login pw", chat_id=8)],
                [message_update(604, "/login other", chat_id=8)],
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store, auth),
    )
    assert script.sent[0]["text"] == telegram_bot.LOGIN_FAIL_TEXT
    assert script.sent[1]["text"] == telegram_bot.LOGIN_FAIL_TEXT
    assert not auth.is_authenticated(8)
    assert not auth.is_authenticated(7)


def test_login_no_argument_gets_usage(store):
    store.add_telegram_user(7, "pw")
    auth = telegram_bot.Auth(store)
    script = run_bot_until_stop(
        Script([[message_update(605, "/login")]]),
        dispatch=telegram_bot.make_dispatch(store, auth),
    )
    assert script.sent[0]["text"] == telegram_bot.LOGIN_USAGE_TEXT
    assert not auth.is_authenticated(7)


def test_login_password_with_spaces(store):
    # the password is everything after the command token
    store.add_telegram_user(7, "my secret pw")
    auth = telegram_bot.Auth(store)
    script = run_bot_until_stop(
        Script([[message_update(606, "/login my secret pw")]]),
        dispatch=telegram_bot.make_dispatch(store, auth),
    )
    assert script.sent[0]["text"] == telegram_bot.LOGIN_OK_TEXT
    assert auth.is_authenticated(7)


def test_board_command_requires_auth_then_works_after_login(store):
    pid = store.create_project("yask")["id"]
    store.create_task(pid, "top secret task")
    store.add_telegram_user(7, "pw")
    auth = telegram_bot.Auth(store)
    script = run_bot_until_stop(
        Script(
            [
                [message_update(611, "/projects")],
                [message_update(612, "/login pw")],
                [message_update(613, "/projects")],
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store, auth),
    )
    # unauthenticated: the gate, no board data
    assert script.sent[0]["text"] == telegram_bot.AUTH_REQUIRED_TEXT
    assert "yask" not in script.sent[0]["text"]
    # login, then the real view — the populated board goes out rich (the
    # list is a rich surface)
    assert script.sent[1]["text"] == telegram_bot.LOGIN_OK_TEXT
    assert len(script.sent) == 2
    assert len(script.sent_rich) == 1
    text = script.sent_rich[0]["rich_message"]["markdown"]
    assert text.startswith("# Projects")
    assert "yask" in text


def test_all_board_commands_gated_for_unauthenticated_chat(store):
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    store.add_telegram_user(7, "pw")
    auth = telegram_bot.Auth(store)
    script = run_bot_until_stop(
        Script(
            [
                [message_update(621, "/tasks")],
                [message_update(622, f"/task yask {t['number']}")],
                [message_update(623, f"/attachment yask {t['number']} {d['plan']['id']}")],
                [message_update(624, "/subscribe yask")],
                [message_update(625, "/unsubscribe yask")],
                [message_update(626, f"/move yask {t['number']} Done")],
                [message_update(627, "/add yask something")],
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store, auth),
    )
    assert len(script.sent) == 7
    for m in script.sent:
        assert m["text"] == telegram_bot.AUTH_REQUIRED_TEXT
    # the gate keeps the data itself: no file is sent for /attachment, no
    # subscription row is created for /subscribe, no task state is changed
    # for /move, no task is created for /add (the seed's four tasks only)
    assert script.sent_files == []
    assert store.list_subscriptions(7) == []
    assert store.get_task(pid, t["number"])["state"] == "In progress"
    assert len(store.list_tasks(pid)) == 4


def test_start_help_whoami_work_unauthenticated(store):
    store.add_telegram_user(7, "pw")
    auth = telegram_bot.Auth(store)
    script = run_bot_until_stop(
        Script(
            [
                [message_update(631, "/start")],
                [message_update(632, "/help")],
                [message_update(633, "/whoami")],
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store, auth),
    )
    assert script.sent[0]["text"] == telegram_bot.START_TEXT
    assert script.sent[1]["text"] == telegram_bot.HELP_TEXT
    assert "/login" in script.sent[1]["text"]
    assert "/whoami" in script.sent[1]["text"]
    # /whoami reveals only the sender's own chat id
    assert script.sent[2]["text"] == (
        "Your chat id is 7. Give it to your yask administrator to get access."
    )
    assert "/login" in telegram_bot.START_TEXT


def test_callback_from_unauthenticated_chat_leaks_nothing(store):
    """The security core: an unauthenticated callback gets the auth notice
    as toast and reply, and no attachment bytes go out."""
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    store.add_telegram_user(13, "pw")
    auth = telegram_bot.Auth(store)
    script = run_bot_until_stop(
        Script(
            [[callback_update(641, f"a:{pid}:{t['number']}:{d['plan']['id']}", chat_id=13)]]
        ),
        dispatch=telegram_bot.make_dispatch(store, auth),
        callback_dispatch=telegram_bot.make_callback_dispatch(store, auth),
    )
    assert script.answered == [
        {
            "callback_query_id": "cbq-641",
            "text": telegram_bot.AUTH_REQUIRED_TEXT,
        }
    ]
    assert script.sent == [
        {"chat_id": 13, "text": telegram_bot.AUTH_REQUIRED_TEXT}
    ]
    assert script.sent_files == []  # no document, no photo
    assert script.edited == []


def test_all_callback_families_gated_until_login(store):
    d = seed_task_view(store)
    pid, t = d["pid"], d["t"]
    store.add_telegram_user(11, "pw")
    auth = telegram_bot.Auth(store)
    dispatch = telegram_bot.make_callback_dispatch(store, auth)
    for payload in (
        f"p:{pid}",
        f"t:{pid}:{t['number']}",
        f"a:{pid}:{t['number']}:{d['plan']['id']}",
        f"s:{pid}",
        f"u:{pid}",
        f"m:{pid}:{t['number']}:{DONE_IDX}",
        f"c:{pid}:{t['number']}:{DONE_IDX}",
        f"x:{pid}:{t['number']}",
        "h",
        "h:p",
        "h:t",
        "h:s",
        "h:a",
        "h:h",
    ):
        action = dispatch(
            callback_update(642, payload, chat_id=11)["callback_query"]
        )
        assert action is not None, payload
        assert action.answer_text == telegram_bot.AUTH_REQUIRED_TEXT, payload
        assert action.reply == telegram_bot.AUTH_REQUIRED_TEXT, payload
        assert action.edit is None, payload
    # the gated s:/h:s presses created no subscription, and the gated m:/c:
    # presses moved no task
    assert store.list_subscriptions(11) == []
    assert store.get_task(pid, t["number"])["state"] == "In progress"
    # after login, the same press reaches the board
    assert auth.authenticate(11, "pw")
    action = dispatch(callback_update(643, f"p:{pid}", chat_id=11)["callback_query"])
    assert action is not None
    assert action.reply != telegram_bot.AUTH_REQUIRED_TEXT


def test_start_menu_ungated_buttons_gated(store):
    """/start is open to everyone; the menu's board buttons are not.

    The #62 end behavior: an unauthenticated chat opens the hub via
    /start (the intro — with its /login guidance — plus the hub's
    keyboard, no gate) and presses a board button inside it; the press
    answers the auth notice as toast and reply, with no board data.
    """
    store.add_telegram_user(7, "pw")
    auth = telegram_bot.Auth(store)
    script = run_bot_until_stop(
        Script(
            [
                [message_update(651, "/start")],
                [callback_update(652, "h:p")],
            ]
        ),
        dispatch=telegram_bot.make_dispatch(store, auth),
        callback_dispatch=telegram_bot.make_callback_dispatch(store, auth),
    )
    # /start is ungated: the intro plus the hub's keyboard goes out
    assert script.sent[0]["chat_id"] == 7
    assert script.sent[0]["text"] == telegram_bot.START_TEXT
    assert script.sent[0]["reply_markup"] == telegram_bot.menu_view().reply_markup
    # the hub's board button is gated: the auth notice as toast and reply
    assert script.answered == [
        {
            "callback_query_id": "cbq-652",
            "text": telegram_bot.AUTH_REQUIRED_TEXT,
        }
    ]
    assert len(script.sent) == 2
    assert script.sent[1] == {
        "chat_id": 7,
        "text": telegram_bot.AUTH_REQUIRED_TEXT,
    }
    assert script.edited == []


def test_notifier_skips_unauthenticated_subscribers(store):
    pid = store.create_project("yask")["id"]
    store.add_telegram_user(1, "pw1")
    store.add_telegram_user(2, "pw2")
    store.subscribe_project(1, pid)
    store.subscribe_project(2, pid)
    store.subscribe_project(3, pid)  # subscribed but never permitted
    auth = telegram_bot.Auth(store)
    auth.authenticate(1, "pw1")  # only chat 1 logged in
    api = FakeAPI()
    notifier = telegram_bot.Notifier(api, store, auth)
    notifier.seed()
    t = _moved(store, pid)
    markup = _notification_markup(pid, t, "working")
    asyncio.run(notifier.check())
    # only the authenticated subscriber receives the notification; the
    # change is not replayed later (the cursor advances past it)
    assert api.sent == [(1, _notification(pid, t, "working", "Review"), markup)]
    asyncio.run(notifier.check())
    assert api.sent == [(1, _notification(pid, t, "working", "Review"), markup)]
