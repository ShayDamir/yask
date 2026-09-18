"""Tests for per-user project visibility in the Telegram bot (task #135).

A chat whose visible-project list is set sees only those projects in
every bot surface — commands, inline buttons and state-change
notifications; a non-visible project behaves as if it does not exist
(the regular not-found wording, so a restricted chat cannot tell a
hidden project from a nonexistent one). No rows at all means
unrestricted (every project visible), including for chats that are not
in the allowlist. Stale subscriptions to non-visible projects are left
in place but inert: hidden from the subscription list, never notified.
"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from yask import telegram_bot
from yask.api import create_app
from yask.store import NotFound

BOT_TOKEN = "12345:TEST"
# Allowlist seeds must pass the store's strength floor (task #130): at
# least 12 characters, at least two character classes.
TEST_PW = "pw-01234567890"
TEST_PW_ALT = "pw-01234567891"


def _project_not_found(ref):
    """The bot's canonical project not-found reply (the hidden-is-
    indistinguishable-from-missing wording)."""
    return f"Project '{ref}' not found. Use /projects to list projects."


# --- store: set / list / visibility --------------------------------------------


def test_set_and_list_roundtrip(store):
    p1 = store.create_project("alpha")["id"]
    p2 = store.create_project("beta")["id"]
    p3 = store.create_project("gamma")["id"]
    store.add_telegram_user(7, TEST_PW)
    # the ids come back sorted and deduped
    result = store.set_telegram_user_projects(7, [p3, p1, p1])
    assert result == {"chat_id": 7, "project_ids": [p1, p3]}
    # the list is name-ordered id+name pairs
    assert store.list_telegram_user_projects(7) == [
        {"id": p1, "name": "alpha"},
        {"id": p3, "name": "gamma"},
    ]


def test_set_replaces_the_previous_list(store):
    p1 = store.create_project("alpha")["id"]
    p2 = store.create_project("beta")["id"]
    p3 = store.create_project("gamma")["id"]
    store.add_telegram_user(7, TEST_PW)
    store.set_telegram_user_projects(7, [p1, p2])
    assert store.set_telegram_user_projects(7, [p3])["project_ids"] == [p3]
    assert store.list_telegram_user_projects(7) == [{"id": p3, "name": "gamma"}]


def test_set_empty_list_clears_the_restriction(store):
    p1 = store.create_project("alpha")["id"]
    store.add_telegram_user(7, TEST_PW)
    store.set_telegram_user_projects(7, [p1])
    assert store.set_telegram_user_projects(7, [])["project_ids"] == []
    assert store.list_telegram_user_projects(7) == []
    assert store.visible_project_ids(7) is None  # unrestricted again


def test_set_and_list_unknown_user_are_not_found(store):
    p1 = store.create_project("alpha")["id"]
    with pytest.raises(NotFound):
        store.set_telegram_user_projects(99, [p1])
    with pytest.raises(NotFound):
        store.list_telegram_user_projects(99)


def test_set_unknown_project_is_not_found_and_leaves_the_list_untouched(store):
    p1 = store.create_project("alpha")["id"]
    p2 = store.create_project("beta")["id"]
    store.add_telegram_user(7, TEST_PW)
    store.set_telegram_user_projects(7, [p1])
    # all ids are validated before any write: the rejected call changes nothing
    with pytest.raises(NotFound):
        store.set_telegram_user_projects(7, [p2, 999])
    assert store.list_telegram_user_projects(7) == [{"id": p1, "name": "alpha"}]


def test_visible_project_ids(store):
    p1 = store.create_project("alpha")["id"]
    p2 = store.create_project("beta")["id"]
    store.add_telegram_user(7, TEST_PW)
    # no rows at all (a newly permitted chat, or one not in the allowlist):
    # None = unrestricted
    assert store.visible_project_ids(7) is None
    assert store.visible_project_ids(99) is None
    store.set_telegram_user_projects(7, [p2, p1])
    assert store.visible_project_ids(7) == [p1, p2]


def test_project_visible_to(store):
    p1 = store.create_project("alpha")["id"]
    p2 = store.create_project("beta")["id"]
    store.add_telegram_user(7, TEST_PW)
    store.set_telegram_user_projects(7, [p1])
    assert store.project_visible_to(7, p1) is True
    assert store.project_visible_to(7, p2) is False
    # an unrestricted chat (no rows — allowed or not) sees everything
    store.add_telegram_user(8, TEST_PW_ALT)
    assert store.project_visible_to(8, p1) is True
    assert store.project_visible_to(8, p2) is True
    assert store.project_visible_to(99, p2) is True


def test_removing_the_user_cascades_the_visibility_rows(store):
    p1 = store.create_project("alpha")["id"]
    p2 = store.create_project("beta")["id"]
    store.add_telegram_user(7, TEST_PW)
    store.set_telegram_user_projects(7, [p1, p2])
    store.remove_telegram_user(7)
    count = store.conn.execute(
        "SELECT COUNT(*) AS c FROM telegram_user_projects"
    ).fetchone()["c"]
    assert count == 0


def test_removing_a_project_cascades_its_visibility_rows(store):
    p1 = store.create_project("alpha")["id"]
    p2 = store.create_project("beta")["id"]
    store.add_telegram_user(7, TEST_PW)
    store.set_telegram_user_projects(7, [p1, p2])
    with store.conn:
        store.conn.execute("DELETE FROM projects WHERE id = ?", (p2,))
    assert store.list_telegram_user_projects(7) == [{"id": p1, "name": "alpha"}]


# --- API: the web UI's management endpoints -------------------------------------


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "visibility.db")
    # loopback base URL (task #85): the enforced app rejects non-loopback
    # Host headers, so the suite must hit it as a local client
    with TestClient(app, base_url="http://127.0.0.1:4304") as c:
        yield c


def test_telegram_user_projects_api_roundtrip(client):
    assert client.post(
        "/api/telegram-users", json={"chat_id": 42, "password": TEST_PW}
    ).status_code == 201
    assert client.post("/api/projects", json={"name": "alpha"}).status_code == 201
    assert client.post("/api/projects", json={"name": "beta"}).status_code == 201

    # a newly permitted chat is unrestricted: the list is empty
    assert client.get("/api/telegram-users/42/projects").json() == []

    r = client.put("/api/telegram-users/42/projects", json={"project_ids": [2]})
    assert r.status_code == 200
    assert r.json() == {"chat_id": 42, "project_ids": [2]}
    assert client.get("/api/telegram-users/42/projects").json() == [
        {"id": 2, "name": "beta"}
    ]

    # an empty list clears the restriction
    r = client.put("/api/telegram-users/42/projects", json={"project_ids": []})
    assert r.status_code == 200
    assert r.json() == {"chat_id": 42, "project_ids": []}
    assert client.get("/api/telegram-users/42/projects").json() == []

    # unknown user (GET and PUT) and unknown project id are 404s
    assert client.get("/api/telegram-users/99/projects").status_code == 404
    assert (
        client.put(
            "/api/telegram-users/99/projects", json={"project_ids": [1]}
        ).status_code
        == 404
    )
    assert (
        client.put(
            "/api/telegram-users/42/projects", json={"project_ids": [1, 999]}
        ).status_code
        == 404
    )


def test_telegram_user_projects_api_requires_a_list(client):
    assert client.post(
        "/api/telegram-users", json={"chat_id": 42, "password": TEST_PW}
    ).status_code == 201
    # the body must be {"project_ids": [...]} — missing or non-list is 422
    assert (
        client.put("/api/telegram-users/42/projects", json={}).status_code == 422
    )
    assert (
        client.put(
            "/api/telegram-users/42/projects", json={"project_ids": "1"}
        ).status_code
        == 422
    )


# --- bot: commands ----------------------------------------------------------------

# A two-project board whose chat 7 is restricted to the first project and
# authenticated; the second project is hidden to it (but exists).


def _restricted_board(store, chat_id=7):
    visible = store.create_project("alpha")
    hidden = store.create_project("beta")
    store.add_telegram_user(chat_id, TEST_PW)
    store.set_telegram_user_projects(chat_id, [visible["id"]])
    auth = telegram_bot.Auth(store)
    assert auth.authenticate(chat_id, TEST_PW)
    return visible, hidden, auth


def _active(store, pid, title):
    t = store.create_task(pid, title)
    store.move_task(pid, t["number"], "In progress")
    return t


def test_projects_view_rich_form_hides_non_visible(store):
    visible, hidden, auth = _restricted_board(store)
    dispatch = telegram_bot.make_dispatch(store, auth)
    reply = dispatch("/projects", chat_id=7)
    assert isinstance(reply, telegram_bot.RichReply)
    assert "alpha" in reply.markdown
    assert "beta" not in reply.markdown
    # the keyboard carries only the visible project's button
    payloads = [
        b["callback_data"]
        for row in reply.reply_markup["inline_keyboard"]
        for b in row
    ]
    assert f"p:{visible['id']}" in payloads
    assert f"p:{hidden['id']}" not in payloads


def test_projects_view_no_chat_context_stays_open(store):
    # No chat context (an inaccessible message): there is no chat to
    # restrict, so no filtering applies and the plain KeyboardReply form
    # is kept — the pre-feature shape for that path.
    visible, hidden, _ = _restricted_board(store)
    dispatch = telegram_bot.make_dispatch(store)
    reply = dispatch("/projects", chat_id=None)
    assert isinstance(reply, telegram_bot.KeyboardReply)
    assert "alpha" in reply.text
    assert "beta" in reply.text


def test_tasks_view_no_arg_skips_non_visible(store):
    visible, hidden, auth = _restricted_board(store)
    _active(store, visible["id"], "visible task")
    _active(store, hidden["id"], "hidden task")
    dispatch = telegram_bot.make_dispatch(store, auth)
    reply = dispatch("/tasks", chat_id=7)
    assert isinstance(reply, telegram_bot.RichReply)
    assert "visible task" in reply.markdown
    assert "hidden task" not in reply.markdown
    assert "beta" not in reply.markdown


def test_tasks_view_arg_not_found_for_non_visible(store):
    visible, hidden, auth = _restricted_board(store)
    _active(store, hidden["id"], "hidden task")
    dispatch = telegram_bot.make_dispatch(store, auth)
    # name and id both get the regular not-found text — indistinguishable
    # from a nonexistent project
    assert dispatch(f"/tasks {hidden['name']}", chat_id=7) == _project_not_found(
        hidden["name"]
    )
    assert dispatch(f"/tasks {hidden['id']}", chat_id=7) == _project_not_found(
        str(hidden["id"])
    )
    assert dispatch("/tasks nosuchproject", chat_id=7) == _project_not_found(
        "nosuchproject"
    )
    # the visible project still resolves
    _active(store, visible["id"], "visible task")
    reply = dispatch(f"/tasks {visible['name']}", chat_id=7)
    assert isinstance(reply, telegram_bot.RichReply)
    assert "visible task" in reply.markdown


def test_backlog_and_blocked_views_hide_non_visible(store):
    visible, hidden, auth = _restricted_board(store)
    store.create_task(visible["id"], "visible backlog")
    store.create_task(hidden["id"], "hidden backlog")
    dispatch = telegram_bot.make_dispatch(store, auth)
    reply = dispatch("/backlog", chat_id=7)
    assert isinstance(reply, telegram_bot.RichReply)
    assert "visible backlog" in reply.markdown
    assert "hidden backlog" not in reply.markdown
    assert "beta" not in reply.markdown
    # the argument form of both views answers not-found for the hidden one
    assert dispatch(f"/backlog {hidden['name']}", chat_id=7) == _project_not_found(
        hidden["name"]
    )
    assert dispatch(f"/blocked {hidden['name']}", chat_id=7) == _project_not_found(
        hidden["name"]
    )


def test_task_view_not_found_for_non_visible(store):
    visible, hidden, auth = _restricted_board(store)
    t = store.create_task(hidden["id"], "hidden task")
    dispatch = telegram_bot.make_dispatch(store, auth)
    assert dispatch(f"/task {hidden['name']} {t['number']}", chat_id=7) == (
        _project_not_found(hidden["name"])
    )
    # the visible project still opens
    tv = store.create_task(visible["id"], "visible task")
    reply = dispatch(f"/task {visible['name']} {tv['number']}", chat_id=7)
    assert isinstance(reply, telegram_bot.RichReply)
    assert "visible task" in reply.markdown


def test_attachment_view_not_found_for_non_visible(store):
    visible, hidden, auth = _restricted_board(store)
    t = store.create_task(hidden["id"], "hidden task")
    meta = store.add_attachment(
        hidden["id"], t["number"], "notes.md", "text/markdown", b"secret"
    )
    dispatch = telegram_bot.make_dispatch(store, auth)
    assert dispatch(
        f"/attachment {hidden['name']} {t['number']} {meta['id']}", chat_id=7
    ) == _project_not_found(hidden["name"])
    # and the visible project's attachments still resolve
    tv = store.create_task(visible["id"], "visible task")
    meta = store.add_attachment(
        visible["id"], tv["number"], "notes.md", "text/markdown", b"open"
    )
    reply = dispatch(
        f"/attachment {visible['name']} {tv['number']} {meta['id']}", chat_id=7
    )
    assert isinstance(reply, telegram_bot.RichReply)
    assert "open" in reply.markdown


def test_write_views_refuse_non_visible_projects(store):
    visible, hidden, auth = _restricted_board(store)
    t = store.create_task(hidden["id"], "hidden task")
    dispatch = telegram_bot.make_dispatch(store, auth)
    assert dispatch(f"/move {hidden['name']} {t['number']} Todo", chat_id=7) == (
        _project_not_found(hidden["name"])
    )
    assert dispatch(f"/add {hidden['name']} new task", chat_id=7) == (
        _project_not_found(hidden["name"])
    )
    assert dispatch(
        f"/describe {hidden['name']} {t['number']} desc", chat_id=7
    ) == _project_not_found(hidden["name"])
    assert dispatch(f"/type {hidden['name']} {t['number']} Bug", chat_id=7) == (
        _project_not_found(hidden["name"])
    )
    # nothing was written to the hidden project
    assert [x["number"] for x in store.list_tasks(hidden["id"])] == [1]
    assert store.get_task(hidden["id"], 1)["state"] == "Backlog"


def test_subscribe_list_hides_stale_subscriptions(store):
    visible, hidden, auth = _restricted_board(store)
    # subscriptions created while the projects were still visible
    store.subscribe_project(7, visible["id"])
    store.subscribe_project(7, hidden["id"])
    dispatch = telegram_bot.make_dispatch(store, auth)
    reply = dispatch("/subscribe", chat_id=7)
    assert isinstance(reply, str)
    assert "alpha" in reply
    assert "beta" not in reply
    # the stale subscription is left in place (hidden, not removed)
    assert sorted(s["project_id"] for s in store.list_subscriptions(7)) == sorted(
        [visible["id"], hidden["id"]]
    )


def test_subscribe_and_unsubscribe_not_found_for_non_visible(store):
    visible, hidden, auth = _restricted_board(store)
    dispatch = telegram_bot.make_dispatch(store, auth)
    assert dispatch(f"/subscribe {hidden['name']}", chat_id=7) == (
        _project_not_found(hidden["name"])
    )
    assert dispatch(f"/unsubscribe {hidden['name']}", chat_id=7) == (
        _project_not_found(hidden["name"])
    )
    # no subscription was created or removed
    assert store.list_subscriptions(7) == []


def test_split_project_skips_hidden_prefixes(store):
    """A visible project whose name is a prefix of a non-visible one still
    resolves: the hidden project is invisible to the split, so a task
    titled like the hidden suffix stays reachable in the visible one."""
    yask = store.create_project("yask")
    yask_web = store.create_project("yask web")
    store.create_task(yask["id"], "web 4")
    store.add_telegram_user(7, TEST_PW)
    store.add_telegram_user(8, TEST_PW_ALT)
    store.set_telegram_user_projects(7, [yask["id"]])  # 8 stays unrestricted
    auth = telegram_bot.Auth(store)
    assert auth.authenticate(7, TEST_PW)
    assert auth.authenticate(8, TEST_PW_ALT)
    dispatch = telegram_bot.make_dispatch(store, auth)
    # chat 7: "yask web" is skipped (non-visible), "yask" wins
    reply = dispatch("/task yask web 4", chat_id=7)
    assert isinstance(reply, telegram_bot.RichReply)
    assert "web 4" in reply.markdown
    # chat 8 (unrestricted): the longest prefix "yask web" wins, and task
    # #4 does not exist there
    assert dispatch("/task yask web 4", chat_id=8) == "Task #4 not found in yask web."


# --- bot: the /attach file channel -------------------------------------------------


class Script:
    """Canned getUpdates responses plus request recording — the slice of
    the bot's Bot API surface the visibility tests need (no rich-send or
    edit failures). ``file_gets``/``file_downloads`` record the /attach
    download path, so a visibility rejection can be asserted to happen
    before any download."""

    def __init__(self, get_updates, file_bytes=None):
        self.get_updates = list(get_updates)
        self.file_bytes = file_bytes
        self.file_gets = []
        self.file_downloads = []
        self.sent = []
        self.sent_rich = []
        self.sent_files = []
        self.answered = []
        self.edited = []
        self.stop = None

    def handler(self, request):
        if request.url.path.startswith("/file/"):
            self.file_downloads.append(request.url.path)
            if self.file_bytes is None:
                return httpx.Response(404, text="no file configured")
            return httpx.Response(200, content=self.file_bytes)
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        if method == "getFile":
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
        if method == "getMe":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"id": 1, "username": "yask_test_bot"}},
            )
        if method == "getUpdates":
            if self.get_updates:
                return httpx.Response(
                    200, json={"ok": True, "result": self.get_updates.pop(0)}
                )
            if self.stop is not None:
                self.stop.set()
            return httpx.Response(200, json={"ok": True, "result": []})
        if method in ("setMyCommands", "getMyCommands", "deleteMyCommands"):
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "sendMessage":
            self.sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
        if method == "sendRichMessage":
            self.sent_rich.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
        if method in ("sendDocument", "sendPhoto"):
            self.sent_files.append(method)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
        if method == "answerCallbackQuery":
            self.answered.append(body)
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "editMessageText":
            self.edited.append(body)
            return httpx.Response(200, json={"ok": True, "result": True})
        raise AssertionError(f"unexpected Bot API method: {method}")


def run_bot_until_stop(script, build_dispatch):
    """Run run_bot against the script until the script drains (sets stop),
    wiring the dispatch through ``build_dispatch(api)`` (the production
    ``make_dispatch(store, auth, api=api)`` seam)."""
    script.stop = asyncio.Event()

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(script.handler))
        api = telegram_bot.BotAPI(BOT_TOKEN, client=client)
        try:
            d = build_dispatch(api)
            await telegram_bot.run_bot(
                api,
                d,
                stop_event=script.stop,
                poll_timeout=1,
                error_delay=0.01,
            )
        finally:
            await client.aclose()

    asyncio.run(go())
    return script


def document_update(update_id, caption=None, chat_id=7):
    """A getUpdates payload entry carrying a document message (the
    ``/attach`` channel)."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id},
            "document": {
                "file_id": "DOC-1",
                "file_name": "notes.md",
                "mime_type": "text/markdown",
                "file_size": 10,
            },
            "caption": caption,
        },
    }


def test_attach_file_to_hidden_project_answers_not_found_without_downloading(store):
    visible, hidden, auth = _restricted_board(store)
    t = store.create_task(hidden["id"], "hidden task")
    script = run_bot_until_stop(
        Script([[document_update(901, caption=f"/attach {hidden['name']} {t['number']}")]]),
        build_dispatch=lambda api: telegram_bot.make_dispatch(store, auth, api=api),
    )
    # the visibility rejection precedes the download: no getFile, no CDN
    assert script.file_gets == []
    assert script.file_downloads == []
    assert script.sent_files == []
    assert len(script.sent) == 1
    assert script.sent[0]["text"] == _project_not_found(hidden["name"])
    # nothing was attached
    assert store.list_attachments(hidden["id"], t["number"]) == []


# --- bot: inline-keyboard callbacks -------------------------------------------------


def _cbq(payload, chat_id=7):
    """The raw callback_query the bot routes to the callback dispatch."""
    return {
        "id": f"cbq-{payload}",
        "data": payload,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id},
            "text": "the message the button lives in",
        },
        "from": {"id": 42, "is_bot": False},
        "chat_instance": "ci-1",
    }


def test_callback_families_not_found_for_non_visible_projects(store):
    visible, hidden, auth = _restricted_board(store)
    t = store.create_task(hidden["id"], "hidden task")
    meta = store.add_attachment(
        hidden["id"], t["number"], "notes.md", "text/markdown", b"secret"
    )
    dispatch = telegram_bot.make_callback_dispatch(store, auth)
    not_found = _project_not_found(hidden["id"])
    for payload in (
        f"p:{hidden['id']}",
        f"t:{hidden['id']}:{t['number']}",
        f"a:{hidden['id']}:{t['number']}:{meta['id']}",
        f"s:{hidden['id']}",
        f"u:{hidden['id']}",
        f"m:{hidden['id']}:{t['number']}:1",
        f"c:{hidden['id']}:{t['number']}:1",
        f"x:{hidden['id']}:{t['number']}",
    ):
        action = dispatch(_cbq(payload))
        assert action is not None, payload
        assert action.reply == not_found, payload
        assert action.edit is None, payload
    # nothing was written: no move, no subscription, no attachment send
    assert store.get_task(hidden["id"], t["number"])["state"] == "Backlog"
    assert store.list_subscriptions(7) == []
    # the visible project's buttons still work (a p: press opens its list)
    action = dispatch(_cbq(f"p:{visible['id']}"))
    assert action is not None
    assert action.reply != not_found


def test_hub_routes_apply_visibility(store):
    visible, hidden, auth = _restricted_board(store)
    _active(store, visible["id"], "visible task")
    _active(store, hidden["id"], "hidden task")
    store.subscribe_project(7, visible["id"])
    store.subscribe_project(7, hidden["id"])
    dispatch = telegram_bot.make_callback_dispatch(store, auth)

    action = dispatch(_cbq("h:p"))
    assert isinstance(action.reply, telegram_bot.RichReply)
    assert "alpha" in action.reply.markdown
    assert "beta" not in action.reply.markdown

    action = dispatch(_cbq("h:t"))
    assert isinstance(action.reply, telegram_bot.RichReply)
    assert "visible task" in action.reply.markdown
    assert "hidden task" not in action.reply.markdown

    # the subscription list hides the stale one
    action = dispatch(_cbq("h:s"))
    assert isinstance(action.reply, str)
    assert "alpha" in action.reply
    assert "beta" not in action.reply


# --- bot: state-change notifications (the Notifier) ---------------------------------


class FakeAPI:
    """Records send_message calls."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return {"message_id": len(self.sent)}


def _moved(store, pid, title="working", to_state="Review"):
    t = store.create_task(pid, title)
    store.move_task(pid, t["number"], to_state, confirm=True)
    return t


def _notification(pid, name, t, title, to_state):
    return (
        f"{name}: #{t['number']} {title} — Backlog → {to_state} "
        f"(/task {pid} {t['number']})"
    )


def _notification_markup(pid, t, title):
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


def test_notifier_skips_subscribers_who_cannot_see_the_project(store):
    visible = store.create_project("alpha")
    other = store.create_project("gamma")
    store.add_telegram_user(7, TEST_PW)
    store.add_telegram_user(8, TEST_PW_ALT)
    # chat 7 is restricted away from "alpha" (it only sees "gamma"); chat 8
    # is unrestricted — both are subscribed and authenticated
    store.set_telegram_user_projects(7, [other["id"]])
    store.subscribe_project(7, visible["id"])
    store.subscribe_project(8, visible["id"])
    auth = telegram_bot.Auth(store)
    assert auth.authenticate(7, TEST_PW)
    assert auth.authenticate(8, TEST_PW_ALT)
    api = FakeAPI()
    notifier = telegram_bot.Notifier(api, store, auth)
    notifier.seed()
    t = _moved(store, visible["id"])
    markup = _notification_markup(visible["id"], t, "working")
    expected = (8, _notification(visible["id"], "alpha", t, "working", "Review"), markup)
    asyncio.run(notifier.check())
    # only the unrestricted subscriber receives the notification; the
    # change is not replayed later (the cursor advances past it)
    assert api.sent == [expected]
    asyncio.run(notifier.check())
    assert api.sent == [expected]


# --- bot: the auth gate still precedes visibility ------------------------------------


def test_auth_gate_precedes_visibility(store):
    visible, hidden, auth = _restricted_board(store)
    dispatch = telegram_bot.make_dispatch(store, auth)
    # a chat not in the allowlist gets the auth notice — not the board
    # data, and not the not-found wording either (the gate is first)
    assert dispatch("/projects", chat_id=99) == telegram_bot.AUTH_REQUIRED_TEXT
    assert dispatch(f"/tasks {hidden['name']}", chat_id=99) == (
        telegram_bot.AUTH_REQUIRED_TEXT
    )
    cb = telegram_bot.make_callback_dispatch(store, auth)
    action = cb(_cbq(f"p:{hidden['id']}", chat_id=99))
    assert action is not None
    assert action.answer_text == telegram_bot.AUTH_REQUIRED_TEXT
    assert action.reply == telegram_bot.AUTH_REQUIRED_TEXT
    assert action.edit is None
    # and a permitted-but-not-logged-in chat is gated the same way
    store.add_telegram_user(10, TEST_PW_ALT)
    assert dispatch("/projects", chat_id=10) == telegram_bot.AUTH_REQUIRED_TEXT
