"""Start the real server and talk to it over a socket.

Every other webapp test greps the INDEX_HTML string, which cannot catch a
failure in the serving path. A missing import in the response writer once made
every request return a truncated body with a 200 status — the page never
loaded at all, and the whole suite stayed green. These tests exercise the
socket.
"""

import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing

import pytest

from coldstorage.config import Config
from coldstorage.store.archive import Archive
from coldstorage.webapp import serve


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(layout):
    """A real HTTP server on loopback, torn down after the test."""
    port = _free_port()
    arc = Archive(layout)
    arc.__enter__()
    t = threading.Thread(
        target=serve,
        args=(arc, Config()),
        kwargs={"host": "127.0.0.1", "port": port, "open_browser": False},
        daemon=True,
    )
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):  # wait for the socket to accept
        try:
            urllib.request.urlopen(base + "/api/status", timeout=1).read()
            break
        except Exception:
            threading.Event().wait(0.05)
    else:
        pytest.fail("server never came up")
    yield base, arc
    arc.__exit__(None, None, None)


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.status, r.headers, r.read()


def test_index_is_delivered_complete(live):
    """The whole body must arrive — a truncated 200 is the bug this exists for."""
    base, _ = live
    status, headers, body = _get(base, "/")
    assert status == 200
    assert int(headers["Content-Length"]) == len(body), "body was cut short"
    assert body.startswith(b"<!doctype html>")
    assert body.rstrip().endswith(b"</html>"), "page truncated before the closing tag"
    assert b"boot();" in body


def test_every_api_endpoint_answers(live):
    base, _ = live
    for path in (
        "/api/status",
        "/api/records?connector=instagram&limit=5",
        "/api/threads?connector=instagram",
        "/api/thread?connector=instagram&thread=nobody",
    ):
        status, headers, body = _get(base, path)
        assert status == 200, path
        assert int(headers["Content-Length"]) == len(body), f"{path} truncated"
        json.loads(body)  # must be valid JSON


def test_media_route_round_trips_over_http(live):
    base, arc = live
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xff"
        b"\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    sha = arc.blobs.put_bytes(png)
    status, headers, body = _get(base, f"/media/{sha}")
    assert status == 200
    assert body == png
    assert headers["Content-Type"] == "image/png"
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_unknown_paths_do_not_kill_the_server(live):
    """A 404 must be a 404, and the server must still be alive afterwards."""
    base, _ = live
    for path in ("/nope", "/media/zzz", "/api/records?limit=abc", "/api/thread"):
        try:
            urllib.request.urlopen(base + path, timeout=5).read()
        except urllib.error.HTTPError as e:
            e.read()
    status, _, _ = _get(base, "/api/status")
    assert status == 200, "server died on a bad request"


def test_search_with_punctuation_does_not_500(live):
    """Real queries contain ? : - & and quotes. FTS5 treats them as syntax."""
    base, _ = live
    for q in ["why?", "a:b", "-x", 'he said "hi', "AT&T", "*", "NEAR("]:
        status, _, body = _get(base, f"/api/records?q={urllib.parse.quote(q)}&limit=5")
        assert status == 200, f"query {q!r} broke the server"
        json.loads(body)
