"""Tests for the Telegram bot process (``yask telegram``).

All Bot API traffic goes through ``httpx.MockTransport`` — real Telegram is
never called.
"""

import asyncio
import json

import httpx

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


class Script:
    """Canned Bot API responses plus request recording.

    Each ``get_updates`` entry is either a list of updates (returned as an ok
    response) or a ``(status, body)`` pair for an error response. Once the
    entries are exhausted, getUpdates returns empty results and sets ``stop``
    (when configured), so a bot run always terminates. ``fail_once_with``
    raises once on the first request to simulate a transport failure.
    """

    def __init__(self, get_updates, get_me_ok=True, fail_once_with=None):
        self.get_updates = list(get_updates)
        self.get_me_ok = get_me_ok
        self.fail_once_with = fail_once_with
        self.failed_once = False
        self.sent = []
        self.offsets = []
        self.stop = None

    def handler(self, request):
        if self.fail_once_with is not None and not self.failed_once:
            self.failed_once = True
            raise self.fail_once_with
        method = request.url.path.rsplit("/", 1)[-1]
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


def run_bot_until_stop(script, dispatch=None, error_delay=0.01):
    """Run run_bot against the script until the script drains (sets stop).

    ``dispatch`` defaults to the static ``reply_for``; pass a
    ``make_dispatch(store)`` dispatcher for store-backed commands.
    """
    script.stop = asyncio.Event()
    if dispatch is None:
        dispatch = telegram_bot.reply_for

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
    asyncio.run(telegram_bot.run_bot(api, telegram_bot.reply_for, stop_event=stop))
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
