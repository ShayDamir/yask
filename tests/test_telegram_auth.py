"""Tests for the Telegram bot's user allowlist (task #49).

Store level: CRUD on ``telegram_users``, salted-hash storage (never
plaintext, never returned), password verification. API level: the four
global endpoints the web UI uses to manage the list.
"""

import pytest
from fastapi.testclient import TestClient

from yask import db
from yask.api import create_app
from yask.store import (
    Conflict,
    NotFound,
    Store,
    ValidationError,
    TELEGRAM_LOGIN_MAX_ATTEMPTS,
)


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "auth.db")
    # loopback base URL (task #85): the enforced app rejects non-loopback
    # Host headers, so the suite must hit it as a local client
    with TestClient(app, base_url="http://127.0.0.1:4304") as c:
        yield c


# --- store: storage and hashing -------------------------------------------------


def test_add_telegram_user_stores_hash_not_plaintext(store):
    user = store.add_telegram_user(111, "s3cret!")
    assert user["chat_id"] == 111
    assert user["created_at"] == user["updated_at"]
    # the result never carries the hash (or the password)
    assert set(user) == {"chat_id", "created_at", "updated_at"}
    row = store.conn.execute(
        "SELECT password_hash FROM telegram_users WHERE chat_id = 111"
    ).fetchone()
    stored = row["password_hash"]
    assert stored != "s3cret!"
    assert "s3cret!" not in stored
    assert stored.startswith("scrypt$")


def test_same_password_different_chats_get_different_hashes(store):
    store.add_telegram_user(1, "same")
    store.add_telegram_user(2, "same")
    rows = {
        r["chat_id"]: r["password_hash"]
        for r in store.conn.execute(
            "SELECT chat_id, password_hash FROM telegram_users"
        )
    }
    # per-user salt: identical passwords hash differently
    assert rows[1] != rows[2]


def test_add_telegram_user_validation(store):
    with pytest.raises(ValidationError):
        store.add_telegram_user(0, "pw")
    with pytest.raises(ValidationError):
        store.add_telegram_user(-5, "pw")
    with pytest.raises(ValidationError):
        store.add_telegram_user(1, "")
    with pytest.raises(ValidationError):
        store.add_telegram_user(1, "   ")
    assert store.list_telegram_users() == []


def test_add_telegram_user_duplicate_is_conflict(store):
    store.add_telegram_user(7, "pw")
    with pytest.raises(Conflict):
        store.add_telegram_user(7, "other")
    # the original password still verifies
    assert store.verify_telegram_user(7, "pw") is True


def test_list_telegram_users_never_exposes_hash(store):
    store.add_telegram_user(20, "a")
    store.add_telegram_user(10, "b")
    users = store.list_telegram_users()
    assert [u["chat_id"] for u in users] == [10, 20]  # chat-id order
    for u in users:
        assert set(u) == {"chat_id", "created_at", "updated_at"}


# --- store: password rotation / removal / verification --------------------------


def test_set_telegram_user_password_rehashes(store):
    created = store.add_telegram_user(7, "old")
    updated = store.set_telegram_user_password(7, "new")
    assert updated["chat_id"] == 7
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["updated_at"]
    assert store.verify_telegram_user(7, "new") is True
    assert store.verify_telegram_user(7, "old") is False  # rotated
    row = store.conn.execute(
        "SELECT password_hash FROM telegram_users WHERE chat_id = 7"
    ).fetchone()
    assert "old" not in row["password_hash"]
    assert "new" not in row["password_hash"]


def test_set_telegram_user_password_unknown_chat_is_not_found(store):
    with pytest.raises(NotFound):
        store.set_telegram_user_password(99, "pw")
    with pytest.raises(ValidationError):
        store.set_telegram_user_password(99, "  ")


def test_remove_telegram_user(store):
    store.add_telegram_user(7, "pw")
    assert store.remove_telegram_user(7) == {"applied": True, "removed": True}
    assert store.list_telegram_users() == []
    assert store.verify_telegram_user(7, "pw") is False
    with pytest.raises(NotFound):
        store.remove_telegram_user(7)


def test_verify_telegram_user(store):
    store.add_telegram_user(7, "pw")
    assert store.verify_telegram_user(7, "pw") is True
    assert store.verify_telegram_user(7, "wrong") is False
    assert store.verify_telegram_user(7, "") is False
    # unknown chat: False, indistinguishable from a wrong password
    assert store.verify_telegram_user(8, "pw") is False


# --- store: the persisted login session (task #55) ------------------------------


def _authenticated_at(store, chat_id=7):
    row = store.conn.execute(
        "SELECT authenticated_at FROM telegram_users WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    return row["authenticated_at"]


def test_login_telegram_user(store):
    store.add_telegram_user(7, "pw")
    assert store.login_telegram_user(7, "pw") is True
    # re-login is idempotent (a re-stamp, not an error)
    assert store.login_telegram_user(7, "pw") is True
    # wrong password, empty password, unknown chat: False, indistinguishable
    assert store.login_telegram_user(7, "wrong") is False
    assert store.login_telegram_user(7, "") is False
    assert store.login_telegram_user(8, "pw") is False


def test_login_telegram_user_stamps_the_row(store):
    store.add_telegram_user(7, "pw")
    assert _authenticated_at(store) is None  # permitted, never logged in
    assert store.login_telegram_user(7, "pw") is True
    assert _authenticated_at(store) is not None


def test_is_telegram_user_authenticated(store):
    store.add_telegram_user(7, "pw")
    assert store.is_telegram_user_authenticated(7) is False  # never logged in
    assert store.is_telegram_user_authenticated(8) is False  # not permitted
    store.login_telegram_user(7, "pw")
    assert store.is_telegram_user_authenticated(7) is True


def test_password_rotation_invalidates_the_session(store):
    store.add_telegram_user(7, "old")
    store.login_telegram_user(7, "old")
    assert store.is_telegram_user_authenticated(7) is True
    store.set_telegram_user_password(7, "new")
    # rotated out: the old password no longer logs in, the new one does
    assert store.is_telegram_user_authenticated(7) is False
    assert store.login_telegram_user(7, "old") is False
    assert store.is_telegram_user_authenticated(7) is False
    assert store.login_telegram_user(7, "new") is True
    assert store.is_telegram_user_authenticated(7) is True


def test_removal_invalidates_the_session(store):
    store.add_telegram_user(7, "pw")
    store.login_telegram_user(7, "pw")
    assert store.is_telegram_user_authenticated(7) is True
    store.remove_telegram_user(7)
    assert store.is_telegram_user_authenticated(7) is False
    assert store.login_telegram_user(7, "pw") is False


def test_login_session_survives_a_reconnect(tmp_path):
    """The session lives in the database: a fresh connection to the same
    file (a bot restart) still sees the chat as authenticated."""
    path = tmp_path / "restart.db"
    conn = db.connect(path)
    store = Store(conn)
    store.add_telegram_user(7, "pw")
    assert store.login_telegram_user(7, "pw") is True
    conn.close()
    conn = db.connect(path)
    restarted = Store(conn)
    try:
        assert restarted.is_telegram_user_authenticated(7) is True
        assert restarted.login_telegram_user(7, "pw") is True
        assert restarted.login_telegram_user(7, "wrong") is False
        assert restarted.is_telegram_user_authenticated(7) is True
    finally:
        conn.close()


# --- API: the web UI's management endpoints -------------------------------------


def test_telegram_users_api_crud(client):
    assert client.get("/api/telegram-users").json() == []

    r = client.post("/api/telegram-users", json={"chat_id": 42, "password": "pw"})
    assert r.status_code == 201
    body = r.json()
    assert body["chat_id"] == 42
    assert "created_at" in body and "updated_at" in body
    assert "password_hash" not in body and "password" not in body

    users = client.get("/api/telegram-users").json()
    assert [u["chat_id"] for u in users] == [42]
    assert "password_hash" not in users[0]

    # duplicate chat is a conflict; bad input is a validation error
    assert client.post("/api/telegram-users", json={"chat_id": 42, "password": "x"}).status_code == 409
    assert client.post("/api/telegram-users", json={"chat_id": 0, "password": "x"}).status_code == 400
    assert client.post("/api/telegram-users", json={"chat_id": -3, "password": "x"}).status_code == 400
    assert client.post("/api/telegram-users", json={"chat_id": 43, "password": " "}).status_code == 400

    # password rotation
    r = client.put("/api/telegram-users/42", json={"password": "new"})
    assert r.status_code == 200
    assert r.json()["chat_id"] == 42
    assert r.json()["created_at"] == body["created_at"]
    # unknown chat / blank password
    assert client.put("/api/telegram-users/99", json={"password": "x"}).status_code == 404
    assert client.put("/api/telegram-users/42", json={"password": ""}).status_code == 400

    # removal
    r = client.delete("/api/telegram-users/42")
    assert r.status_code == 200
    assert r.json() == {"applied": True, "removed": True}
    assert client.get("/api/telegram-users").json() == []
    assert client.delete("/api/telegram-users/42").status_code == 404


def test_telegram_users_api_password_verifiable(tmp_path):
    """A password created through the API is accepted by the store."""
    app = create_app(tmp_path / "auth.db")
    # loopback base URL (task #85): the enforced app rejects non-loopback
    # Host headers, so the suite must hit it as a local client
    with TestClient(app, base_url="http://127.0.0.1:4304") as c:
        r = c.post("/api/telegram-users", json={"chat_id": 42, "password": "via-api"})
        assert r.status_code == 201
        assert c.put("/api/telegram-users/42", json={"password": "rotated"}).status_code == 200
    conn = db.connect(tmp_path / "auth.db")
    try:
        s = Store(conn)
        assert s.verify_telegram_user(42, "rotated") is True
        assert s.verify_telegram_user(42, "via-api") is False
        assert s.verify_telegram_user(43, "rotated") is False
        # the DB holds only a hash — the plaintext appears nowhere
        row = conn.execute(
            "SELECT password_hash FROM telegram_users WHERE chat_id = 42"
        ).fetchone()
        assert "rotated" not in row["password_hash"]
    finally:
        conn.close()


# --- store: login throttle / lockout (task #69) -------------------------------


def _tg_failures(store, chat_id=7):
    row = store.conn.execute(
        "SELECT login_failures, locked_until FROM telegram_users WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    return row["login_failures"], row["locked_until"]


def _set_locked_until(store, chat_id, value):
    store.conn.execute(
        "UPDATE telegram_users SET locked_until = ? WHERE chat_id = ?",
        (value, chat_id),
    )


def test_login_locks_out_after_threshold():
    store = Store(db.connect(":memory:"))
    store.add_telegram_user(7, "pw")
    # the threshold failures: 5 wrong passwords
    for _ in range(TELEGRAM_LOGIN_MAX_ATTEMPTS):
        assert store.login_telegram_user(7, "wrong") is False
    assert _tg_failures(store)[0] == TELEGRAM_LOGIN_MAX_ATTEMPTS
    assert _tg_failures(store)[1] is not None  # a lockout stamp is set
    # the 6th attempt — even the *correct* password — is locked out
    assert store.login_telegram_user(7, "pw") is False
    locked = _tg_failures(store)[1]
    assert locked is not None and locked > db.utcnow()


def test_locked_out_returns_false_without_scrypt():
    """Once locked, the correct password still fails — proving the throttle
    gates *before* the scrypt cost, not by simply getting a wrong answer."""
    store = Store(db.connect(":memory:"))
    store.add_telegram_user(7, "pw")
    for _ in range(TELEGRAM_LOGIN_MAX_ATTEMPTS):
        store.login_telegram_user(7, "wrong")
    # force a far-future lock: the attempt must fail without ever verifying
    _set_locked_until(store, 7, "2999-01-01T00:00:00Z")
    assert store.login_telegram_user(7, "pw") is False


def test_lockout_expires():
    store = Store(db.connect(":memory:"))
    store.add_telegram_user(7, "pw")
    for _ in range(TELEGRAM_LOGIN_MAX_ATTEMPTS):
        store.login_telegram_user(7, "wrong")
    # simulate the window elapsing: a past ``locked_until`` is not in force
    _set_locked_until(store, 7, "2000-01-01T00:00:00Z")
    assert store.login_telegram_user(7, "pw") is True


def test_success_resets_failure_counter():
    store = Store(db.connect(":memory:"))
    store.add_telegram_user(7, "pw")
    for _ in range(TELEGRAM_LOGIN_MAX_ATTEMPTS - 2):
        store.login_telegram_user(7, "wrong")
    assert _tg_failures(store)[0] == TELEGRAM_LOGIN_MAX_ATTEMPTS - 2
    assert store.login_telegram_user(7, "pw") is True
    # the counter is cleared on success — a fresh window begins
    assert _tg_failures(store)[0] == 0
    assert _tg_failures(store)[1] is None


def test_unknown_chat_is_not_locked():
    """Failed attempts against an unknown chat stay indistinguishable from a
    wrong password and never touch a row."""
    store = Store(db.connect(":memory:"))
    for _ in range(TELEGRAM_LOGIN_MAX_ATTEMPTS + 3):
        assert store.login_telegram_user(12345, "pw") is False
    # no row was ever created
    assert store.conn.execute(
        "SELECT COUNT(*) AS c FROM telegram_users WHERE chat_id = 12345"
    ).fetchone()["c"] == 0


def test_lockout_is_independently_keyed_by_chat():
    store = Store(db.connect(":memory:"))
    store.add_telegram_user(7, "pw")
    store.add_telegram_user(8, "pw")
    for _ in range(TELEGRAM_LOGIN_MAX_ATTEMPTS):
        store.login_telegram_user(7, "wrong")
    # chat 7 is locked...
    assert store.login_telegram_user(7, "pw") is False
    # ...but chat 8, a different chat, still logs in
    assert store.login_telegram_user(8, "pw") is True
