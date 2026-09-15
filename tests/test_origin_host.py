"""Security regression tests (task #85): the unauthenticated loopback REST
API must reject traffic that cannot be a legitimate local browser or local
client — cross-origin requests (CSRF, CWE-352) and non-loopback Host
headers (DNS rebinding, CWE-350). Non-browser clients (no Origin header)
keep working."""

import pytest
from fastapi.testclient import TestClient

from yask.api import CSP, create_app


@pytest.fixture()
def client(tmp_path):
    # Enforced app (the default) addressed as a local client: the
    # TestClient sends Host: 127.0.0.1:4304, which the checks accept.
    app = create_app(tmp_path / "security.db")
    with TestClient(app, base_url="http://127.0.0.1:4304") as c:
        yield c


@pytest.fixture()
def relaxed_client(tmp_path):
    # create_app(allow_remote=True): the checks are off — the same hostile
    # requests must succeed here, proving the checks are what blocked them
    # (and that --allow-remote keeps working for remote clients).
    app = create_app(tmp_path / "relaxed.db", allow_remote=True)
    with TestClient(app, base_url="http://127.0.0.1:4304") as c:
        yield c


def test_cross_site_multipart_upload_rejected(client):
    """The named vector: a CORS-simple multipart form POST from an attacker
    page reaches the attachments endpoint without preflight; the browser's
    cross-site Origin must be rejected and nothing stored."""
    pid = client.post("/api/projects", json={"name": "Sec Demo"}).json()["id"]
    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    before = client.get(f"/api/projects/{pid}/tasks/1/attachments").json()

    r = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        files={"file": ("evil.svg", b"<svg/>", "image/svg+xml")},
        headers={"Origin": "http://evil.com"},
    )
    assert r.status_code == 403
    assert client.get(f"/api/projects/{pid}/tasks/1/attachments").json() == before


def test_cross_origin_json_post_rejected(client):
    r = client.post(
        "/api/projects",
        json={"name": "sneaky"},
        headers={"Origin": "http://evil.com"},
    )
    assert r.status_code == 403
    assert client.get("/api/projects").json() == []


def test_cross_origin_delete_rejected(client):
    pid = client.post("/api/projects", json={"name": "Del"}).json()["id"]
    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    att = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        files={"file": ("a.md", b"a", "text/markdown")},
    ).json()
    r = client.delete(
        f"/api/attachments/{att['id']}", headers={"Origin": "http://evil.com"}
    )
    assert r.status_code == 403
    # nothing was deleted
    assert client.get(f"/api/attachments/{att['id']}").status_code == 200


def test_cross_origin_get_rejected(client):
    """Cross-origin reads are rejected as defense in depth (today's GETs
    are side-effect-free, but a rebinding page must not read either)."""
    client.post("/api/projects", json={"name": "G"})
    r = client.get("/api/projects", headers={"Origin": "http://evil.com"})
    assert r.status_code == 403


def test_dns_rebinding_read_rejected(client):
    r = client.get("/api/projects", headers={"Host": "attacker.com:4304"})
    assert r.status_code == 403


def test_dns_rebinding_write_rejected(client):
    """Same "origin" from the browser's perspective (the Origin check alone
    would pass) — the Host check catches it."""
    before = client.get("/api/projects").json()
    r = client.post(
        "/api/projects",
        json={"name": "rebound"},
        headers={
            "Host": "attacker.com:4304",
            "Origin": "http://attacker.com:4304",
        },
    )
    assert r.status_code == 403
    assert client.get("/api/projects").json() == before


def test_same_origin_post_allowed(client):
    r = client.post(
        "/api/projects",
        json={"name": "Local"},
        headers={"Origin": "http://127.0.0.1:4304"},
    )
    assert r.status_code == 201
    assert client.get("/api/projects").json()[0]["name"] == "Local"


def test_loopback_host_variants_allowed(client):
    r = client.post(
        "/api/projects",
        json={"name": "Localhost"},
        headers={"Host": "localhost:4304", "Origin": "http://localhost:4304"},
    )
    assert r.status_code == 201
    # IPv6 loopback arrives bracketed; no Origin (non-browser client)
    r = client.get("/api/projects", headers={"Host": "[::1]:4304"})
    assert r.status_code == 200


def test_origin_port_and_value_rejections(client):
    client.post("/api/projects", json={"name": "P"})
    # different port = different origin
    r = client.get("/api/projects", headers={"Origin": "http://127.0.0.1:4305"})
    assert r.status_code == 403
    # opaque origin (sandboxed iframe et al.)
    r = client.get("/api/projects", headers={"Origin": "null"})
    assert r.status_code == 403
    # unparseable origin
    r = client.get("/api/projects", headers={"Origin": "javascript:alert(1)"})
    assert r.status_code == 403


def test_sec_fetch_site_cross_site_rejected(client):
    client.post("/api/projects", json={"name": "P"})
    r = client.get("/api/projects", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    # with a hostile Origin on top (defense in depth)
    r = client.get(
        "/api/projects",
        headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://evil.com"},
    )
    assert r.status_code == 403


def test_allow_remote_disables_checks(relaxed_client):
    r = relaxed_client.get(
        "/api/projects",
        headers={"Host": "attacker.com:4304", "Origin": "http://attacker.com:4304"},
    )
    assert r.status_code == 200
    r = relaxed_client.post(
        "/api/projects",
        json={"name": "remote"},
        headers={"Origin": "http://evil.com"},
    )
    assert r.status_code == 201


def test_rejection_carries_security_headers(client):
    """The 403 the check returns still carries the four #86 security
    headers — pins the middleware-ordering decision (the check must stay
    inner to the headers middleware)."""
    r = client.get("/api/projects", headers={"Host": "attacker.com:4304"})
    assert r.status_code == 403
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
