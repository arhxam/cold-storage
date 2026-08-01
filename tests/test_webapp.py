import json

from coldstorage.config import Config
from coldstorage.engine import Engine
from coldstorage.store.archive import Archive
from coldstorage.webapp import api_records, api_thread, api_threads, handle


def test_index_served(layout):
    with Archive(layout) as arc:
        code, ctype, body = handle("/", {}, arc, Config())
    assert code == 200
    assert "text/html" in ctype
    assert b"Cold Storage" in body


def test_api_status(layout, instagram_export):
    with Archive(layout) as arc:
        Engine(arc).ingest(instagram_export)
        code, ctype, body = handle("/api/status", {}, arc, Config())
    assert code == 200 and "json" in ctype
    data = json.loads(body)
    assert data["total_records"] > 0
    assert any(c["connector"] == "instagram" for c in data["connectors"])
    assert "total_bytes_h" in data


def test_api_records_and_search(layout, instagram_export):
    with Archive(layout) as arc:
        Engine(arc).ingest(instagram_export)
        # all records for connector
        rows = api_records(arc, {"connector": ["instagram"]})
        assert rows and all(r["connector"] == "instagram" for r in rows)
        # search narrows results
        hits = api_records(arc, {"connector": ["instagram"], "q": ["café"]})
        assert any("café" in (r.get("text") or "") for r in hits)


def test_api_threads_and_thread(layout, instagram_export):
    with Archive(layout) as arc:
        Engine(arc).ingest(instagram_export)
        data = api_threads(arc, "instagram")
        assert "self" in data
        assert any(t["thread"] == "Beyoncé" for t in data["threads"])  # mojibake-repaired
        assert data["type_counts"].get("follower", 0) >= 1
        msgs = api_thread(arc, "instagram", "Beyoncé")
        assert msgs and all(m["type"] == "message" for m in msgs)


def test_api_records_type_filter(layout, instagram_export):
    with Archive(layout) as arc:
        Engine(arc).ingest(instagram_export)
        followers = api_records(arc, {"connector": ["instagram"], "type": ["follower"]})
        assert followers and all(r["type"] == "follower" for r in followers)


def test_threads_route(layout, instagram_export):
    with Archive(layout) as arc:
        Engine(arc).ingest(instagram_export)
        code, ctype, body = handle("/api/threads", {"connector": ["instagram"]}, arc, Config())
    assert code == 200 and "json" in ctype
    import json as _json

    assert "threads" in _json.loads(body)


def test_404(layout):
    with Archive(layout) as arc:
        code, _, _ = handle("/nope", {}, arc, Config())
    assert code == 404


def test_server_starts_and_responds(layout, instagram_export):
    """Integration: bind an ephemeral port, hit it over HTTP, then shut down."""
    import threading
    import urllib.request
    from http.server import HTTPServer
    from urllib.parse import parse_qs, urlparse

    from coldstorage.webapp import handle as route

    with Archive(layout) as arc:
        Engine(arc).ingest(instagram_export)
        cfg = Config()

        from http.server import BaseHTTPRequestHandler

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                p = urlparse(self.path)
                code, ctype, body = route(p.path, parse_qs(p.query), arc, cfg)
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(body)

        httpd = HTTPServer(("127.0.0.1", 0), H)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            body = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read()
            assert b"Cold Storage" in body
            status = json.loads(
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5).read()
            )
            assert status["total_records"] > 0
        finally:
            httpd.shutdown()
