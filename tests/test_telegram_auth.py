"""Tests for the Telegram bot's user allowlist (task #49).

Store level: CRUD on ``telegram_users``, salted-hash storage (never
plaintext, never returned), password verification. API level: the four
global endpoints the web UI uses to manage the list.
"""

import statistics
import time
from datetime import datetime, timedelta, timezone

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
    TELEGRAM_MIN_PASSWORD_LENGTH,
    TELEGRAM_SESSION_TTL_SECONDS,
)

# Passwords that satisfy the strength floor (task #130): at least 12
# characters, at least two character classes. Rotation tests need two
# distinct ones.
TEST_PW = "pw-01234567890"  # 14 chars, 3 classes
OLD_PW = "pw-01234567890"  # rotation seed
NEW_PW = "Pw-9876543210"  # 13 chars, 3 classes; distinct from OLD_PW


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "auth.db")
    # loopback base URL (task #85): the enforced app rejects non-loopback
    # Host headers, so the suite must hit it as a local client
    with TestClient(app, base_url="http://127.0.0.1:4304") as c:
        yield c


# --- store: storage and hashing -------------------------------------------------


def test_add_telegram_user_stores_hash_not_plaintext(store):
    user = store.add_telegram_user(111, TEST_PW)
    assert user["chat_id"] == 111
    assert user["created_at"] == user["updated_at"]
    # the result never carries the hash (or the password)
    assert set(user) == {"chat_id", "created_at", "updated_at"}
    row = store.conn.execute(
        "SELECT password_hash FROM telegram_users WHERE chat_id = 111"
    ).fetchone()
    stored = row["password_hash"]
    assert stored != TEST_PW
    assert TEST_PW not in stored
    assert stored.startswith("scrypt$")


def test_same_password_different_chats_get_different_hashes(store):
    store.add_telegram_user(1, TEST_PW)
    store.add_telegram_user(2, TEST_PW)
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
        store.add_telegram_user(0, TEST_PW)
    with pytest.raises(ValidationError):
        store.add_telegram_user(-5, TEST_PW)
    with pytest.raises(ValidationError):
        store.add_telegram_user(1, "")
    with pytest.raises(ValidationError):
        store.add_telegram_user(1, "   ")
    assert store.list_telegram_users() == []


def test_add_telegram_user_password_length_boundary(store):
    # one char under the floor: rejected on length
    with pytest.raises(
        ValidationError, match=f"at least {TELEGRAM_MIN_PASSWORD_LENGTH}"
    ):
        store.add_telegram_user(1, "a1234567890")
    # exactly the floor with two character classes: accepted
    assert store.add_telegram_user(1, "a12345678901")["chat_id"] == 1


def test_add_telegram_user_requires_two_character_classes(store):
    # long enough, but a single character class: rejected on the class floor
    with pytest.raises(ValidationError, match="character classes"):
        store.add_telegram_user(1, "aaaaaaaaaaaa")
    with pytest.raises(ValidationError, match="character classes"):
        store.add_telegram_user(1, "123456789012")
    # two classes: accepted
    assert store.add_telegram_user(1, "a12345678901")["chat_id"] == 1


def test_add_telegram_user_duplicate_is_conflict(store):
    store.add_telegram_user(7, TEST_PW)
    with pytest.raises(Conflict):
        store.add_telegram_user(7, NEW_PW)
    # the original password still verifies
    assert store.verify_telegram_user(7, TEST_PW) is True


def test_list_telegram_users_never_exposes_hash(store):
    store.add_telegram_user(20, TEST_PW)
    store.add_telegram_user(10, NEW_PW)
    users = store.list_telegram_users()
    assert [u["chat_id"] for u in users] == [10, 20]  # chat-id order
    for u in users:
        assert set(u) == {"chat_id", "created_at", "updated_at"}


# --- store: password rotation / removal / verification --------------------------


def test_set_telegram_user_password_rehashes(store):
    created = store.add_telegram_user(7, OLD_PW)
    updated = store.set_telegram_user_password(7, NEW_PW)
    assert updated["chat_id"] == 7
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["updated_at"]
    assert store.verify_telegram_user(7, NEW_PW) is True
    assert store.verify_telegram_user(7, OLD_PW) is False  # rotated
    row = store.conn.execute(
        "SELECT password_hash FROM telegram_users WHERE chat_id = 7"
    ).fetchone()
    assert OLD_PW not in row["password_hash"]
    assert NEW_PW not in row["password_hash"]


def test_set_telegram_user_password_rejects_weak_password(store):
    created = store.add_telegram_user(7, TEST_PW)
    with pytest.raises(ValidationError, match="at least 12"):
        store.set_telegram_user_password(7, "short")
    with pytest.raises(ValidationError, match="character classes"):
        store.set_telegram_user_password(7, "aaaaaaaaaaaa")
    # the rotation never happened: the old password still verifies and
    # updated_at is unchanged
    assert store.verify_telegram_user(7, TEST_PW) is True
    row = store.conn.execute(
        "SELECT updated_at FROM telegram_users WHERE chat_id = 7"
    ).fetchone()
    assert row["updated_at"] == created["updated_at"]


def test_set_telegram_user_password_unknown_chat_is_not_found(store):
    with pytest.raises(NotFound):
        store.set_telegram_user_password(99, TEST_PW)
    with pytest.raises(ValidationError):
        store.set_telegram_user_password(99, "  ")


def test_remove_telegram_user(store):
    store.add_telegram_user(7, TEST_PW)
    assert store.remove_telegram_user(7) == {"applied": True, "removed": True}
    assert store.list_telegram_users() == []
    assert store.verify_telegram_user(7, TEST_PW) is False
    with pytest.raises(NotFound):
        store.remove_telegram_user(7)


def test_verify_telegram_user(store):
    store.add_telegram_user(7, TEST_PW)
    assert store.verify_telegram_user(7, TEST_PW) is True
    # the verify path has no strength check: a weak password still fails
    # like any wrong one
    assert store.verify_telegram_user(7, "wrong") is False
    assert store.verify_telegram_user(7, "") is False
    # unknown chat: False, indistinguishable from a wrong password
    assert store.verify_telegram_user(8, TEST_PW) is False


# --- store: the persisted login session (task #55) ------------------------------


def _authenticated_at(store, chat_id=7):
    row = store.conn.execute(
        "SELECT authenticated_at FROM telegram_users WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    return row["authenticated_at"]


def test_login_telegram_user(store):
    store.add_telegram_user(7, TEST_PW)
    assert store.login_telegram_user(7, TEST_PW) is True
    # re-login is idempotent (a re-stamp, not an error)
    assert store.login_telegram_user(7, TEST_PW) is True
    # wrong password, empty password, unknown chat: False, indistinguishable
    assert store.login_telegram_user(7, "wrong") is False
    assert store.login_telegram_user(7, "") is False
    assert store.login_telegram_user(8, TEST_PW) is False


def test_login_telegram_user_stamps_the_row(store):
    store.add_telegram_user(7, TEST_PW)
    assert _authenticated_at(store) is None  # permitted, never logged in
    assert store.login_telegram_user(7, TEST_PW) is True
    assert _authenticated_at(store) is not None


def test_is_telegram_user_authenticated(store):
    store.add_telegram_user(7, TEST_PW)
    assert store.is_telegram_user_authenticated(7) is False  # never logged in
    assert store.is_telegram_user_authenticated(8) is False  # not permitted
    store.login_telegram_user(7, TEST_PW)
    assert store.is_telegram_user_authenticated(7) is True


def test_password_rotation_invalidates_the_session(store):
    store.add_telegram_user(7, OLD_PW)
    store.login_telegram_user(7, OLD_PW)
    assert store.is_telegram_user_authenticated(7) is True
    store.set_telegram_user_password(7, NEW_PW)
    # rotated out: the old password no longer logs in, the new one does
    assert store.is_telegram_user_authenticated(7) is False
    assert store.login_telegram_user(7, OLD_PW) is False
    assert store.is_telegram_user_authenticated(7) is False
    assert store.login_telegram_user(7, NEW_PW) is True
    assert store.is_telegram_user_authenticated(7) is True


def test_removal_invalidates_the_session(store):
    store.add_telegram_user(7, TEST_PW)
    store.login_telegram_user(7, TEST_PW)
    assert store.is_telegram_user_authenticated(7) is True
    store.remove_telegram_user(7)
    assert store.is_telegram_user_authenticated(7) is False
    assert store.login_telegram_user(7, TEST_PW) is False


def test_login_session_survives_a_reconnect(tmp_path):
    """The session lives in the database: a fresh connection to the same
    file (a bot restart) still sees the chat as authenticated."""
    path = tmp_path / "restart.db"
    conn = db.connect(path)
    store = Store(conn)
    store.add_telegram_user(7, TEST_PW)
    assert store.login_telegram_user(7, TEST_PW) is True
    conn.close()
    conn = db.connect(path)
    restarted = Store(conn)
    try:
        assert restarted.is_telegram_user_authenticated(7) is True
        assert restarted.login_telegram_user(7, TEST_PW) is True
        assert restarted.login_telegram_user(7, "wrong") is False
        assert restarted.is_telegram_user_authenticated(7) is True
    finally:
        conn.close()


# --- store: session TTL and /logout (task #131) --------------------------------


def _backdate_session(store, chat_id, seconds):
    """Set ``authenticated_at`` to ``seconds`` before now.

    Second-precision timestamps (``db.utcnow()``'s format), so TTL
    boundary tests always use a margin of at least a minute — never exact
    equality.
    """
    stamp = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.conn.execute(
        "UPDATE telegram_users SET authenticated_at = ? WHERE chat_id = ?",
        (stamp, chat_id),
    )


def test_login_session_expires_after_ttl(store):
    store.add_telegram_user(7, TEST_PW)
    assert store.login_telegram_user(7, TEST_PW) is True
    assert store.is_telegram_user_authenticated(7) is True
    # backdate the stamp past the cutoff: the session has expired
    _backdate_session(store, 7, TELEGRAM_SESSION_TTL_SECONDS + 60)
    assert store.is_telegram_user_authenticated(7) is False
    # a fresh /login re-stamps the window
    assert store.login_telegram_user(7, TEST_PW) is True
    assert store.is_telegram_user_authenticated(7) is True


def test_login_session_within_ttl_is_live(store):
    store.add_telegram_user(7, TEST_PW)
    assert store.login_telegram_user(7, TEST_PW) is True
    # just inside the window (cutoff + a minute of margin): still live
    _backdate_session(store, 7, TELEGRAM_SESSION_TTL_SECONDS - 60)
    assert store.is_telegram_user_authenticated(7) is True


def test_logout_telegram_user(store):
    store.add_telegram_user(7, TEST_PW)
    assert store.login_telegram_user(7, TEST_PW) is True
    assert store.logout_telegram_user(7) is True
    assert _authenticated_at(store) is None
    assert store.is_telegram_user_authenticated(7) is False
    # no session left to revoke: a second logout is a no-op
    assert store.logout_telegram_user(7) is False
    # a fresh /login works after a logout
    assert store.login_telegram_user(7, TEST_PW) is True
    assert store.is_telegram_user_authenticated(7) is True


def test_logout_unknown_or_unlogged_in_is_false_and_no_error(store):
    store.add_telegram_user(7, TEST_PW)
    # unknown chat: False, no NotFound — the bot must not reveal
    # allowlist membership (task #129's stance)
    assert store.logout_telegram_user(8) is False
    # permitted but never logged in: False, no error
    assert store.logout_telegram_user(7) is False


def test_touch_telegram_session(store):
    store.add_telegram_user(7, TEST_PW)
    assert store.login_telegram_user(7, TEST_PW) is True
    # past the cutoff: expired — a touch (authenticated activity) moves
    # the stamp back into the live window
    _backdate_session(store, 7, TELEGRAM_SESSION_TTL_SECONDS + 60)
    assert store.is_telegram_user_authenticated(7) is False
    assert store.touch_telegram_session(7) is True
    assert store.is_telegram_user_authenticated(7) is True
    # no session to touch: False, the stamp stays NULL
    store.add_telegram_user(9, TEST_PW)  # permitted, never logged in
    assert store.touch_telegram_session(9) is False
    assert _authenticated_at(store, 9) is None
    # a NULLed stamp (logged out / rotated) is never resurrected
    assert store.logout_telegram_user(7) is True
    assert store.touch_telegram_session(7) is False
    assert _authenticated_at(store) is None
    assert store.is_telegram_user_authenticated(7) is False


# --- API: the web UI's management endpoints -------------------------------------


def test_telegram_users_api_crud(client):
    assert client.get("/api/telegram-users").json() == []

    r = client.post("/api/telegram-users", json={"chat_id": 42, "password": TEST_PW})
    assert r.status_code == 201
    body = r.json()
    assert body["chat_id"] == 42
    assert "created_at" in body and "updated_at" in body
    assert "password_hash" not in body and "password" not in body

    users = client.get("/api/telegram-users").json()
    assert [u["chat_id"] for u in users] == [42]
    assert "password_hash" not in users[0]

    # duplicate chat is a conflict; bad input is a validation error
    assert client.post("/api/telegram-users", json={"chat_id": 42, "password": NEW_PW}).status_code == 409
    assert client.post("/api/telegram-users", json={"chat_id": 0, "password": TEST_PW}).status_code == 400
    assert client.post("/api/telegram-users", json={"chat_id": -3, "password": TEST_PW}).status_code == 400
    assert client.post("/api/telegram-users", json={"chat_id": 43, "password": " "}).status_code == 400
    # a weak password is a validation error (the strength floor, task #130)
    assert client.post("/api/telegram-users", json={"chat_id": 45, "password": "short"}).status_code == 400

    # password rotation
    r = client.put("/api/telegram-users/42", json={"password": NEW_PW})
    assert r.status_code == 200
    assert r.json()["chat_id"] == 42
    assert r.json()["created_at"] == body["created_at"]
    # unknown chat / blank or weak password
    assert client.put("/api/telegram-users/99", json={"password": TEST_PW}).status_code == 404
    assert client.put("/api/telegram-users/42", json={"password": ""}).status_code == 400
    assert client.put("/api/telegram-users/42", json={"password": "short"}).status_code == 400

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
        r = c.post("/api/telegram-users", json={"chat_id": 42, "password": TEST_PW})
        assert r.status_code == 201
        assert c.put("/api/telegram-users/42", json={"password": NEW_PW}).status_code == 200
    conn = db.connect(tmp_path / "auth.db")
    try:
        s = Store(conn)
        assert s.verify_telegram_user(42, NEW_PW) is True
        assert s.verify_telegram_user(42, TEST_PW) is False
        assert s.verify_telegram_user(43, NEW_PW) is False
        # the DB holds only a hash — the plaintext appears nowhere
        row = conn.execute(
            "SELECT password_hash FROM telegram_users WHERE chat_id = 42"
        ).fetchone()
        assert NEW_PW not in row["password_hash"]
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
    store.add_telegram_user(7, TEST_PW)
    # the threshold failures: 5 wrong passwords
    for _ in range(TELEGRAM_LOGIN_MAX_ATTEMPTS):
        assert store.login_telegram_user(7, "wrong") is False
    assert _tg_failures(store)[0] == TELEGRAM_LOGIN_MAX_ATTEMPTS
    assert _tg_failures(store)[1] is not None  # a lockout stamp is set
    # the 6th attempt — even the *correct* password — is locked out
    assert store.login_telegram_user(7, TEST_PW) is False
    locked = _tg_failures(store)[1]
    assert locked is not None and locked > db.utcnow()


def test_locked_out_returns_false_without_scrypt():
    """Once locked, the correct password still fails — proving the throttle
    gates *before* the scrypt cost, not by simply getting a wrong answer."""
    store = Store(db.connect(":memory:"))
    store.add_telegram_user(7, TEST_PW)
    for _ in range(TELEGRAM_LOGIN_MAX_ATTEMPTS):
        store.login_telegram_user(7, "wrong")
    # force a far-future lock: the attempt must fail without ever verifying
    _set_locked_until(store, 7, "2999-01-01T00:00:00Z")
    assert store.login_telegram_user(7, TEST_PW) is False


def test_lockout_expires():
    store = Store(db.connect(":memory:"))
    store.add_telegram_user(7, TEST_PW)
    for _ in range(TELEGRAM_LOGIN_MAX_ATTEMPTS):
        store.login_telegram_user(7, "wrong")
    # simulate the window elapsing: a past ``locked_until`` is not in force
    _set_locked_until(store, 7, "2000-01-01T00:00:00Z")
    assert store.login_telegram_user(7, TEST_PW) is True


def test_success_resets_failure_counter():
    store = Store(db.connect(":memory:"))
    store.add_telegram_user(7, TEST_PW)
    for _ in range(TELEGRAM_LOGIN_MAX_ATTEMPTS - 2):
        store.login_telegram_user(7, "wrong")
    assert _tg_failures(store)[0] == TELEGRAM_LOGIN_MAX_ATTEMPTS - 2
    assert store.login_telegram_user(7, TEST_PW) is True
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
    store.add_telegram_user(7, TEST_PW)
    store.add_telegram_user(8, TEST_PW)
    for _ in range(TELEGRAM_LOGIN_MAX_ATTEMPTS):
        store.login_telegram_user(7, "wrong")
    # chat 7 is locked...
    assert store.login_telegram_user(7, TEST_PW) is False
    # ...but chat 8, a different chat, still logs in
    assert store.login_telegram_user(8, TEST_PW) is True


# --- store: unknown-chat decoy / timing (task #129) ---------------------------


def test_unknown_chat_costs_as_much_as_wrong_password():
    """An unknown chat id must take about as long as a wrong password:
    response time must not reveal allowlist membership (task #129)."""
    store = Store(db.connect(":memory:"))
    store.add_telegram_user(7, TEST_PW)
    # warm up on an *unknown* chat only: triggers the one-time decoy
    # generation (2x scrypt) outside the measured window, and deliberately
    # leaves chat 7's failure counter at 0 — a burned counter would make the
    # lock land on the 4th measured attempt and the 5th would fast-path
    store.login_telegram_user(8, "warm")
    unknown = []
    for _ in range(5):
        start = time.perf_counter()
        store.login_telegram_user(8, "pw")
        unknown.append(time.perf_counter() - start)
    # the 5 wrong attempts reach the lockout threshold exactly on the 5th —
    # the lock stamp is written *after* that attempt's verification, so all
    # 5 measurements pay the full scrypt
    wrong = []
    for _ in range(5):
        start = time.perf_counter()
        store.login_telegram_user(7, "wrong")
        wrong.append(time.perf_counter() - start)
    # relative band (machine/CI-speed independent): a regression to a fast
    # early-return would collapse the unknown side to ~0.1 ms vs ~100 ms
    median_unknown = statistics.median(unknown)
    median_wrong = statistics.median(wrong)
    assert 0.5 * median_wrong <= median_unknown <= 1.5 * median_wrong
