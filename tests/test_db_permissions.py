"""The SQLite data directory and files are machine-private (0700/0600).

The DB holds the whole board, attachment BLOBs, and the Telegram
allowlist's scrypt password hashes, so a world-readable file (the
umask-022 default) is a data leak on multi-user machines.
"""

import os
import stat
from pathlib import Path

from yask import db


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_fresh_connect_creates_machine_private_modes(tmp_path):
    db_path = tmp_path / "data" / "yask.db"
    conn = db.connect(db_path)
    try:
        assert _mode(db_path.parent) == 0o700
        assert _mode(db_path) == 0o600
        # WAL sidecars live while the connection is open.
        assert _mode(db_path.with_name("yask.db-wal")) == 0o600
        assert _mode(db_path.with_name("yask.db-shm")) == 0o600
    finally:
        conn.close()


def test_connect_repairs_existing_world_readable_modes(tmp_path):
    db_path = tmp_path / "data" / "yask.db"
    conn = db.connect(db_path)
    conn.close()

    # Simulate a legacy install created under umask 022.
    os.chmod(db_path.parent, 0o755)
    os.chmod(db_path, 0o644)

    conn = db.connect(db_path)
    try:
        assert _mode(db_path.parent) == 0o700
        assert _mode(db_path) == 0o600
    finally:
        conn.close()


def test_in_memory_connect_leaves_cwd_mode_unchanged(tmp_path, monkeypatch):
    # Path(":memory:").parent is Path("."), so an unguarded chmod pass would
    # 0700 the process's current working directory on every in-memory
    # connect — an unintended side effect on multi-user machines.
    cwd = tmp_path / "workdir"
    cwd.mkdir()
    os.chmod(cwd, 0o755)
    monkeypatch.chdir(cwd)

    conn = db.connect(":memory:")
    try:
        assert _mode(cwd) == 0o755
    finally:
        conn.close()
    assert _mode(cwd) == 0o755
