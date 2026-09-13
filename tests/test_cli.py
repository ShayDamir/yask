"""Tests for the loopback-only serve guard in ``yask.cli``."""

import argparse

import pytest

from yask.cli import cmd_serve, host_bind_verdict


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "ok"),
        ("::1", "ok"),
        ("localhost", "ok"),
        ("0.0.0.0", "refuse"),
        ("192.168.1.10", "refuse"),
        ("10.0.0.5", "refuse"),
        ("8.8.8.8", "refuse"),
    ],
)
def test_host_bind_verdict_loopback(host, expected):
    assert host_bind_verdict(host, allow_remote=False) == expected


def test_host_bind_verdict_remote_override():
    assert host_bind_verdict("0.0.0.0", allow_remote=True) == "warn"
    assert host_bind_verdict("127.0.0.1", allow_remote=False) == "ok"


def _serve_args(**overrides):
    base = dict(host="127.0.0.1", port=4304, data="/tmp/yask-cli-test-data")
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cmd_serve_refuses_remote(monkeypatch):
    # Unset any ambient override so the test is deterministic.
    monkeypatch.delenv("YASK_ALLOW_REMOTE", raising=False)
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    rc = cmd_serve(_serve_args(host="0.0.0.0", allow_remote=False))
    assert rc == 2


def test_cmd_serve_loopback_succeeds(monkeypatch):
    monkeypatch.delenv("YASK_ALLOW_REMOTE", raising=False)
    called = {}
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: called.update(ran=True))
    rc = cmd_serve(_serve_args(host="127.0.0.1", allow_remote=False))
    assert rc == 0
    assert called.get("ran") is True


def test_cmd_serve_remote_override_succeeds(monkeypatch):
    monkeypatch.delenv("YASK_ALLOW_REMOTE", raising=False)
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    rc = cmd_serve(_serve_args(host="0.0.0.0", allow_remote=True))
    assert rc == 0


def test_cmd_serve_remote_override_via_env(monkeypatch):
    monkeypatch.setenv("YASK_ALLOW_REMOTE", "1")
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    rc = cmd_serve(_serve_args(host="0.0.0.0", allow_remote=False))
    assert rc == 0
