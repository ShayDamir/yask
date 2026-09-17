"""Pixel-bomb guard (task #127, CWE-400): an upload whose bitmap declares a
canvas beyond ``Store.MAX_IMAGE_PIXELS`` must be rejected at the store
level. The 10 MB encoded cap is not enough — a few-KB PNG may declare a
30000x30000 canvas that decodes to ~3.6 GB of pixels, freezing / OOMing the
web viewer's tab of whoever opens the attachment.

Headers are crafted byte-by-byte with ``struct`` (no real encoder): the
store reads headers only, never decodes. Covers all four attachment bitmaps
(PNG, JPEG, GIF, WEBP in its VP8 / VP8L / VP8X chunk forms) at the store,
REST, MCP, and bot levels; the bot regression lives in test_telegram_bot.py.
"""

import asyncio
import base64
import json
import struct

import pytest
from fastapi.testclient import TestClient
from mcp.types import TextContent

from yask.api import create_app
from yask.mcp_server import build_server
from yask.store import Store, ValidationError

# A real 1x1 transparent PNG (the same constant test_mcp.py uses; duplicated
# here — test files stay self-contained).
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


# -- byte-level crafting helpers ---------------------------------------------


def make_png(w: int, h: int) -> bytes:
    """Signature + IHDR with the declared canvas (CRC/IDAT omitted — the
    guard reads the header only)."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", w, h)
        + b"\x08\x06\x00\x00\x00"
    )


def make_jpeg(w: int, h: int) -> bytes:
    """SOI + APP0 + SOF0 with the declared canvas in the SOF segment."""
    app0 = (
        b"\xff\xe0"
        + struct.pack(">H", 16)
        + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    )
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">H", 11)
        + b"\x08"
        + struct.pack(">HH", h, w)
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + app0 + sof0


def make_gif(w: int, h: int) -> bytes:
    """Header + logical screen descriptor with the declared canvas."""
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00\x00\x00"


def make_webp_vp8(w: int, h: int) -> bytes:
    """Lossy VP8 chunk: start code + frame tag, then w/h as LE16 (14-bit
    fields, so declared dimensions cap at 16383)."""
    payload = b"\x9d\x01\x2a" + b"\x00" * 3 + struct.pack(
        "<HH", w & 0x3FFF, h & 0x3FFF
    )
    chunk = b"VP8 " + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def make_webp_vp8l(w: int, h: int) -> bytes:
    """Lossless VP8L chunk: signature byte + 32-bit bitstream (w-1 in bits
    0-13, h-1 in bits 14-27; 14-bit fields, dims cap at 16383)."""
    v = ((h - 1) << 14) | (w - 1)
    payload = b"\x2f" + struct.pack("<I", v)
    chunk = b"VP8L" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def make_webp_vp8x(w: int, h: int) -> bytes:
    """Extended VP8X chunk (the spec's "VP9X"): flags, then w-1 / h-1 as
    24-bit little-endian."""
    payload = (
        b"\x00\x00\x00\x01"
        + (w - 1).to_bytes(3, "little")
        + (h - 1).to_bytes(3, "little")
    )
    chunk = b"VP8X" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


# -- store level --------------------------------------------------------------

# Oversized canvases per format. VP8 / VP8L carry their dimensions in 14-bit
# fields (max 16383 per side — 16383x16383 still far over the cap), so their
# largest declared canvas is used.
OVERSIZED = [
    pytest.param("image/png", make_png(30000, 30000), id="png"),
    pytest.param("image/jpeg", make_jpeg(30000, 30000), id="jpeg"),
    pytest.param("image/gif", make_gif(30000, 30000), id="gif"),
    pytest.param("image/webp", make_webp_vp8(16383, 16383), id="webp-vp8"),
    pytest.param("image/webp", make_webp_vp8l(16383, 16383), id="webp-vp8l"),
    pytest.param("image/webp", make_webp_vp8x(30000, 30000), id="webp-vp8x"),
]

NORMAL = [
    pytest.param("image/png", make_png(1920, 1080), id="png"),
    pytest.param("image/jpeg", make_jpeg(1920, 1080), id="jpeg"),
    pytest.param("image/gif", make_gif(1920, 1080), id="gif"),
    pytest.param("image/webp", make_webp_vp8(1920, 1080), id="webp-vp8"),
    pytest.param("image/webp", make_webp_vp8l(1920, 1080), id="webp-vp8l"),
    pytest.param("image/webp", make_webp_vp8x(1920, 1080), id="webp-vp8x"),
]


@pytest.mark.parametrize(("content_type", "data"), OVERSIZED)
def test_oversized_header_rejected(store, project, content_type, data):
    t = store.create_task(project["id"], "t")
    with pytest.raises(ValidationError, match="MPixel"):
        store.add_attachment(
            project["id"], t["number"], "bomb.bin", content_type, data
        )
    assert store.list_attachments(project["id"], t["number"]) == []


@pytest.mark.parametrize(("content_type", "data"), NORMAL)
def test_normal_dimensions_accepted(store, project, content_type, data):
    t = store.create_task(project["id"], "t")
    meta = store.add_attachment(
        project["id"], t["number"], "img.bin", content_type, data
    )
    _, stored = store.get_attachment(meta["id"])
    assert stored == data


def test_pixel_cap_boundary_is_strict_greater(store, project):
    t = store.create_task(project["id"], "t")
    edge = make_png(5000, 5000)  # exactly Store.MAX_IMAGE_PIXELS
    assert 5000 * 5000 == Store.MAX_IMAGE_PIXELS
    meta = store.add_attachment(
        project["id"], t["number"], "edge.png", "image/png", edge
    )
    _, stored = store.get_attachment(meta["id"])
    assert stored == edge
    over = make_png(5000, 5001)  # 25,005,000 px: one pixel too many
    with pytest.raises(ValidationError, match="MPixel"):
        store.add_attachment(
            project["id"], t["number"], "over.png", "image/png", over
        )
    assert len(store.list_attachments(project["id"], t["number"])) == 1


@pytest.mark.parametrize(
    ("content_type", "data"),
    [
        # PNG signature only
        ("image/png", b"\x89PNG\r\n\x1a\n"),
        # first chunk is not IHDR (the shape test_archive_history.py pins)
        ("image/png", b"\x89PNG\r\n\x1a\n" + b"0" * 100),
        # JPEG SOI but no SOF
        ("image/jpeg", b"\xff\xd8"),
        ("image/jpeg", b"\xff\xd8\xff\xe0\x00\x00"),
        # GIF header truncated at 8 bytes
        ("image/gif", b"GIF89a\x00"),
        # RIFF container that is not WEBP
        ("image/webp", b"RIFF\x24\x00\x00\x00AVIF"),
        # a real 1x1 PNG
        ("image/png", PNG_1X1),
    ],
)
def test_undecipherable_header_fails_open(store, project, content_type, data):
    """A header the parser cannot decipher is accepted: a browser cannot
    decode a headerless bitmap into a bomb either."""
    t = store.create_task(project["id"], "t")
    meta = store.add_attachment(
        project["id"], t["number"], "x.bin", content_type, data
    )
    _, stored = store.get_attachment(meta["id"])
    assert stored == data


def test_non_image_types_unaffected(store, project):
    t = store.create_task(project["id"], "t")
    meta = store.add_attachment(
        project["id"], t["number"], "notes.md", "text/markdown", b"# hi"
    )
    _, stored = store.get_attachment(meta["id"])
    assert stored == b"# hi"


# -- REST level ---------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    # Enforced app (the default) addressed as a local client: the TestClient
    # sends Host: 127.0.0.1:4304, which the origin/host checks accept.
    app = create_app(tmp_path / "pixel-bomb.db")
    with TestClient(app, base_url="http://127.0.0.1:4304") as c:
        yield c


def _rest_task(client) -> int:
    pid = client.post("/api/projects", json={"name": "Sec"}).json()["id"]
    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    return pid


def test_rest_rejects_pixel_bomb(client):
    pid = _rest_task(client)
    before = client.get(f"/api/projects/{pid}/tasks/1/attachments").json()
    r = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        files={"file": ("bomb.png", make_png(30000, 30000), "image/png")},
    )
    assert r.status_code == 400
    assert "MPixel" in r.json()["detail"]
    assert client.get(f"/api/projects/{pid}/tasks/1/attachments").json() == before


def test_rest_accepts_normal_image(client):
    pid = _rest_task(client)
    r = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        files={"file": ("ok.png", make_png(1920, 1080), "image/png")},
    )
    assert r.status_code == 201
    assert len(client.get(f"/api/projects/{pid}/tasks/1/attachments").json()) == 1


# -- MCP level -----------------------------------------------------------------


def _call(store, name, **args):
    server = build_server(store)
    blocks = asyncio.run(server.call_tool(name, args))
    assert blocks
    # call_tool returns either a flat list of content blocks or a tuple
    # (blocks, structured_result); normalize to the flat list.
    return blocks[0] if isinstance(blocks, tuple) else blocks


def _texts(blocks):
    return [c for c in blocks if isinstance(c, TextContent)]


def test_mcp_rejects_pixel_bomb(store, project):
    t = store.create_task(project["id"], "t")
    res = _call(
        store,
        "add_attachment",
        project=project["name"],
        number=t["number"],
        filename="bomb.png",
        data_base64=base64.b64encode(make_png(30000, 30000)).decode(),
        content_type="image/png",
    )
    data = json.loads(_texts(res)[0].text)
    assert data["ok"] is False
    assert "MPixel" in data["error"]
    assert store.list_attachments(project["id"], t["number"]) == []
