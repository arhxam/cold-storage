"""A local web app: a chat-first archive browser, served on 127.0.0.1 only.

This is the visual face of Cold Storage without shipping an Electron bundle. A
tiny stdlib HTTP server (no framework, no telemetry, loopback-only) serves a
single-page messaging UI that reads the local archive and exposes a few JSON
endpoints. Nothing leaves the machine.

Routing is a pure function (:func:`handle`) so it is trivially testable without
binding a socket.
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Config
from .status import compute_status, human_bytes
from .store.archive import Archive

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def api_status(archive: Archive, config: Config) -> dict:
    st = compute_status(archive, config)
    d = asdict(st)
    d["total_bytes_h"] = human_bytes(st.total_bytes)
    d["type_counts"] = {
        c.connector: archive.index.counts_by_type(c.connector) for c in st.connectors
    }
    return d


def api_records(archive: Archive, qs: dict[str, list[str]]) -> list[dict]:
    connector = (qs.get("connector") or [None])[0]
    type_ = (qs.get("type") or [None])[0]
    query = (qs.get("q") or [""])[0]
    limit = _int_arg(qs, "limit", default=300, lo=1, hi=10000)
    if query:
        return archive.index.search(query, connector=connector or None, limit=limit)
    return archive.index.records_page(connector=connector, type_=type_, limit=limit)


def _int_arg(qs: dict[str, list[str]], name: str, *, default: int, lo: int, hi: int) -> int:
    """Query-string integers come from a URL, so they can be anything."""
    try:
        return max(lo, min(hi, int((qs.get(name) or [str(default)])[0])))
    except (TypeError, ValueError):
        return default


def api_threads(archive: Archive, connector: str) -> dict:
    """Conversation list + type breakdown for the chat UI."""
    return {
        "self": archive.index.self_author(connector),
        "threads": archive.index.threads(connector),
        "type_counts": archive.index.counts_by_type(connector),
    }


def api_thread(archive: Archive, connector: str, thread: str) -> list[dict]:
    return archive.index.thread_messages(connector, thread)


#: Content types we will hand back for a stored blob. Anything not on this list
#: is served as a download rather than rendered, so a hostile file inside an
#: export can never be interpreted as HTML/SVG in the page's own origin.
_MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "heic": "image/heic",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "wav": "audio/wav",
}


def _sniff_media_type(data: bytes, hint: str | None) -> str:
    """Type from the bytes themselves; the filename is only a tiebreaker.

    Trusting a filename from inside someone's export would let a file called
    ``x.png`` be served as whatever it claimed. Magic numbers cannot lie.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand[:3] == b"hei" or brand[:3] == b"mif":
            return "image/heic"
        if brand[:3] == b"qt ":
            return "video/quicktime"
        return "video/mp4"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:3] == b"ID3" or data[:2] == b"\xff\xfb":
        return "audio/mpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "video/webm"
    ext = (hint or "").rsplit(".", 1)[-1].lower()
    # Only honour the hint for types we would have sniffed anyway.
    if ext in _MEDIA_TYPES and _MEDIA_TYPES[ext] in {
        "image/heic",
        "audio/mp4",
        "video/quicktime",
    }:
        return _MEDIA_TYPES[ext]
    return "application/octet-stream"


def api_media(archive: Archive, sha: str, hint: str | None) -> tuple[int, str, bytes]:
    """Serve one decrypted blob by content hash."""
    # A sha256 hex digest and nothing else: this value indexes straight into the
    # blob store, so anything that is not exactly a hash is refused rather than
    # normalised.
    if not sha or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha.lower()):
        return 400, "text/plain", b"bad blob id"
    try:
        data = archive.blobs.get_bytes(sha.lower())
    except FileNotFoundError:
        return 404, "text/plain", b"not found"
    except Exception:
        # Wrong key, corrupt blob — a broken thumbnail beats a broken page.
        return 404, "text/plain", b"unavailable"
    return 200, _sniff_media_type(data, hint), data


def handle(
    path: str, qs: dict[str, list[str]], archive: Archive, config: Config
) -> tuple[int, str, bytes]:
    """Pure router: returns (status_code, content_type, body)."""
    if path in ("/", "/index.html"):
        return 200, "text/html; charset=utf-8", INDEX_HTML.encode("utf-8")
    if path.startswith("/media/"):
        return api_media(archive, path[len("/media/") :], (qs.get("n") or [None])[0])
    if path == "/favicon.ico":
        # Chromium requests this automatically when no external favicon is
        # declared. Keep the entirely-local UI quiet without adding another
        # asset route or network dependency.
        return 204, "image/x-icon", b""
    if path == "/api/status":
        return 200, "application/json", json.dumps(api_status(archive, config)).encode()
    if path == "/api/records":
        return 200, "application/json", json.dumps(api_records(archive, qs)).encode()
    if path == "/api/threads":
        connector = (qs.get("connector") or [""])[0]
        return 200, "application/json", json.dumps(api_threads(archive, connector)).encode()
    if path == "/api/thread":
        connector = (qs.get("connector") or [""])[0]
        thread = (qs.get("thread") or [""])[0]
        return 200, "application/json", json.dumps(api_thread(archive, connector, thread)).encode()
    return 404, "text/plain", b"not found"


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def serve(
    archive: Archive,
    config: Config,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = True,
) -> None:  # pragma: no cover - exercised via manual/integration use
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default logging
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            code, ctype, body = handle(parsed.path, parse_qs(parsed.query), archive, config)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # Never let a file that came out of someone's export be interpreted
            # as markup in this page's origin, and never let it be sniffed into
            # something other than what we said it is.
            self.send_header("X-Content-Type-Options", "nosniff")
            if parsed.path.startswith("/media/"):
                self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
                self.send_header("Content-Disposition", "inline")
                # Blobs are addressed by content hash, so they can never change.
                self.send_header("Cache-Control", "private, max-age=31536000, immutable")
            self.end_headers()
            # The browser cancelling a request (scrolling away from an image,
            # closing the tab) is normal, not an error worth surfacing.
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

    httpd = HTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# The single-page UI — a chat-first archive browser
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cold Storage</title>
<style>
  /* ------------------------------------------------------------------ */
  /* Design tokens — shadcn/ui "zinc dark". Flat, minimal, high contrast. */
  /* ------------------------------------------------------------------ */
  :root{
    --bg:#09090b;            /* background      (zinc-950) */
    --rail:#09090b;          /* left rail surface          */
    --panel:#18181b;         /* card            (zinc-900) */
    --panel-2:#27272a;       /* muted / raised  (zinc-800) */
    --hover:#1c1c1f;         /* hover wash                 */
    --active:#27272a;        /* selected wash              */
    --line:#27272a;          /* border          (zinc-800) */
    --line-2:#3f3f46;        /* stronger border (zinc-700) */
    --text:#fafafa;          /* foreground      (zinc-50)  */
    --text-2:#a1a1aa;        /* muted-fg        (zinc-400) */
    --text-3:#71717a;        /* subtle          (zinc-500) */
    --brand:#fafafa;         /* primary = near-white       */
    --brand-strong:#e4e4e7;
    --on-brand:#18181b;      /* text on the primary button */
    --accent:#fafafa;
    --accent-soft:rgba(250,250,250,.10);
    --ring:#52525b;          /* focus ring      (zinc-600) */
    --good:#4ade80; --warn:#fbbf24; --bad:#f87171;
    --bubble-them:#27272a;
    --bubble-them-line:#3f3f46;
    --bubble-me:#fafafa;
    --bubble-me-text:#18181b;
    --shadow-card:0 1px 3px rgba(0,0,0,.4);
    --r-sm:6px; --r-md:8px; --r-lg:10px;
  }
  *{ box-sizing:border-box; }
  html,body{ height:100%; }
  body{ margin:0; background:var(--bg); color:var(--text); display:flex; flex-direction:column;
        font:13.5px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
        -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility; }
  button{ font:inherit; color:inherit; background:none; border:0; padding:0; text-align:left; cursor:pointer; }
  code{ font:11.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  [hidden]{ display:none !important; }
  ::-webkit-scrollbar{ width:10px; height:10px; }
  ::-webkit-scrollbar-thumb{ background:#2e2b26; border-radius:8px; border:3px solid transparent; background-clip:content-box; }
  ::-webkit-scrollbar-thumb:hover{ background:#3a362f; border:3px solid transparent; background-clip:content-box; }
  ::-webkit-scrollbar-track{ background:transparent; }
  ::selection{ background:rgba(250,250,250,.18); }
  :focus-visible{ outline:2px solid var(--ring); outline-offset:2px; border-radius:var(--r-sm); }

  .app{ display:grid; grid-template-columns:246px minmax(280px,330px) 1fr; flex:1; min-height:0; }
  .app.mode-dash{ grid-template-columns:246px 1fr; }

  /* ------------------------------------------------------------------ */
  /* Integrated title bar — draggable; hosts the macOS traffic lights    */
  /* so the window controls read as part of the app, not a separate strip */
  /* ------------------------------------------------------------------ */
  .titlebar{ height:44px; flex:none; display:flex; align-items:center; gap:10px;
             padding:0 12px 0 14px; background:var(--rail); border-bottom:1px solid var(--line);
             -webkit-app-region:drag; user-select:none; }
  body.electron .titlebar{ padding-left:82px; }  /* clear the traffic lights */
  .titlebar .tb-brand{ display:flex; align-items:center; gap:9px; font-weight:650;
                       font-size:13.5px; letter-spacing:-.01em; white-space:nowrap; }
  .titlebar .tb-brand .mark{ width:22px; height:22px; border-radius:6px; flex:none; color:var(--on-brand);
                             background:var(--brand); display:grid; place-items:center; }
  .titlebar .sp{ flex:1; }
  .tbtn{ -webkit-app-region:no-drag; display:inline-flex; align-items:center; gap:6px; height:30px;
         padding:0 12px; border-radius:var(--r-md); font-size:12.5px; font-weight:500; color:var(--text);
         border:1px solid var(--line); background:transparent;
         transition:background-color .13s ease, border-color .13s ease, color .13s ease; }
  .tbtn:hover{ background:var(--panel-2); }
  .tbtn.primary{ color:var(--on-brand); border-color:transparent; background:var(--brand); font-weight:550; }
  .tbtn.primary:hover{ background:var(--brand-strong); }
  .tbtn.icon{ padding:0; width:30px; justify-content:center; border-color:transparent; color:var(--text-2); }
  .tbtn.icon:hover{ color:var(--text); }
  .tbtn:disabled{ opacity:.5; pointer-events:none; }

  /* Popover menu (top-right ⋯) */
  .menu{ position:fixed; z-index:60; min-width:214px; background:var(--panel-2); border:1px solid var(--line-2);
         border-radius:11px; box-shadow:0 14px 40px rgba(0,0,0,.5); padding:6px; }
  .menu button{ display:flex; align-items:center; gap:10px; width:100%; padding:8px 10px;
                border-radius:7px; font-size:12.5px; color:var(--text); }
  .menu button:hover{ background:var(--hover); }
  .menu button .ico{ color:var(--text-3); display:grid; place-items:center; }
  .menu .sep{ height:1px; background:var(--line); margin:5px 4px; }

  /* Toasts (in-app backup feedback) */
  .toasts{ position:fixed; right:18px; bottom:18px; z-index:80; display:flex; flex-direction:column;
           gap:10px; align-items:flex-end; }
  .toast{ display:flex; align-items:flex-start; gap:11px; background:var(--panel-2); border:1px solid var(--line-2);
          border-radius:12px; padding:12px 15px; box-shadow:0 14px 40px rgba(0,0,0,.5); max-width:380px;
          animation:toastin .22s ease; }
  @keyframes toastin{ from{ opacity:0; transform:translateY(8px); } }
  .toast .ti{ width:26px; height:26px; border-radius:8px; flex:none; display:grid; place-items:center; margin-top:1px; }
  .toast.good .ti{ color:var(--good); background:rgba(130,201,143,.12); }
  .toast.bad .ti{ color:var(--bad); background:rgba(217,120,98,.12); }
  .toast.work .ti{ color:var(--accent); background:var(--accent-soft); }
  .toast .tt{ font-size:12.5px; color:var(--text); min-width:0; line-height:1.45; }
  .toast .tt b{ font-weight:650; }
  .toast .tsp{ width:15px; height:15px; border-radius:50%; border:2px solid var(--line-2);
               border-top-color:var(--accent); animation:spin .8s linear infinite; }

  /* ------------------------------------------------------------------ */
  /* Accounts view — connect once, then it runs itself                   */
  /* ------------------------------------------------------------------ */
  .connect{ padding:34px 38px; overflow-y:auto; height:100%; }
  .connectin{ max-width:940px; margin:0 auto; }

  /* Card primitive (shadcn Card) */
  .card{ background:var(--panel); border:1px solid var(--line); border-radius:var(--r-lg); }
  .acct{ display:flex; align-items:center; gap:13px; padding:14px 16px; border-bottom:1px solid var(--line); }
  .acct:last-child{ border-bottom:0; }
  .acct .amid{ flex:1; min-width:0; }
  .acct .an{ font-weight:550; font-size:13.5px; letter-spacing:-.01em; }
  .acct .as{ font-size:11.5px; color:var(--text-3); margin-top:2px; display:flex; align-items:center; gap:6px; }
  .acct .aacts{ display:flex; align-items:center; gap:8px; flex:none; }

  /* Status pill (shadcn Badge) */
  .pill{ display:inline-flex; align-items:center; gap:5px; font-size:10.5px; font-weight:550; line-height:1.5;
         padding:2px 8px; border-radius:999px; border:1px solid var(--line-2); color:var(--text-2); white-space:nowrap; }
  .pill.ok{ color:var(--good); border-color:rgba(74,222,128,.3); background:rgba(74,222,128,.08); }
  .pill.wait{ color:var(--warn); border-color:rgba(251,191,36,.3); background:rgba(251,191,36,.08); }
  .pill.err{ color:var(--bad); border-color:rgba(248,113,113,.3); background:rgba(248,113,113,.08); }
  .pill .lamp{ width:5px; height:5px; border-radius:50%; background:currentColor; flex:none; }
  .pill .lamp.pulse{ animation:pulse 1.4s ease-in-out infinite; }
  @keyframes pulse{ 50%{ opacity:.35; } }

  /* Select (shadcn Select, native for reliability) */
  .sel{ -webkit-appearance:none; appearance:none; height:30px; padding:0 26px 0 10px; border-radius:var(--r-md);
        border:1px solid var(--line); background:transparent; color:var(--text); font:inherit; font-size:12px;
        cursor:pointer; background-image:url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23a1a1aa' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
        background-repeat:no-repeat; background-position:right 8px center; }
  .sel:hover{ background-color:var(--panel-2); }

  /* Switch (shadcn Switch) */
  .sw{ width:34px; height:20px; border-radius:999px; background:var(--panel-2); border:1px solid var(--line-2);
       position:relative; flex:none; transition:background-color .16s ease; cursor:pointer; }
  .sw::after{ content:''; position:absolute; top:2px; left:2px; width:14px; height:14px; border-radius:50%;
              background:var(--text-2); transition:transform .16s ease, background-color .16s ease; }
  .sw.on{ background:var(--brand); border-color:var(--brand); }
  .sw.on::after{ transform:translateX(14px); background:var(--on-brand); }

  .pbtn{ height:30px; display:inline-flex; align-items:center; justify-content:center; gap:6px; padding:0 12px;
         border-radius:var(--r-md); font-size:12px; font-weight:500; border:1px solid var(--line); color:var(--text);
         background:transparent; transition:background-color .13s ease, color .13s ease; white-space:nowrap; }
  .pbtn:hover{ background:var(--panel-2); }
  .pbtn.solid{ color:var(--on-brand); border-color:transparent; background:var(--brand); font-weight:550; }
  .pbtn.solid:hover{ background:var(--brand-strong); }
  .pbtn.ghost{ border-color:transparent; color:var(--text-3); }
  .pbtn.ghost:hover{ color:var(--text); background:var(--panel-2); }
  .pbtn:disabled{ opacity:.5; pointer-events:none; }
  .pbtn .spin{ width:12px; height:12px; border-radius:50%; border:1.6px solid var(--line-2);
               border-top-color:var(--text); animation:spin .8s linear infinite; }

  /* Manual-only platforms */
  .manual{ display:flex; align-items:center; gap:12px; padding:12px 16px; border-bottom:1px solid var(--line); }
  .manual:last-child{ border-bottom:0; }
  .manual .mm{ flex:1; min-width:0; }
  .manual .mn{ font-weight:500; font-size:13px; }
  .manual .mr{ font-size:11.5px; color:var(--text-3); margin-top:1px; }

  /* Hero row: add a file by hand */
  .addrow{ display:flex; align-items:center; gap:13px; padding:14px 16px; width:100%; text-align:left; }
  .addrow .ab-ic{ width:34px; height:34px; border-radius:var(--r-md); flex:none; color:var(--text-2);
                  background:var(--panel-2); display:grid; place-items:center; }
  .addrow .ab-t{ font-weight:550; font-size:13px; display:block; }
  .addrow .ab-s{ font-size:11.5px; color:var(--text-3); margin-top:2px; display:block; }
  .addrow .ab-s code{ color:var(--text-2); }

  /* ------------------------------------------------------------------ */
  /* Left rail                                                           */
  /* ------------------------------------------------------------------ */
  .rail{ background:var(--rail); border-right:1px solid var(--line); padding:16px 12px; overflow-y:auto; overflow-x:hidden; }
  .nav-item{ display:flex; align-items:center; gap:10px; width:100%; padding:8px 10px;
             border-radius:var(--r-sm); margin-bottom:2px; font-size:13px; color:var(--text-2);
             transition:background-color .13s ease,color .13s ease; }
  .nav-item .ico{ width:22px; height:22px; flex:none; display:grid; place-items:center; color:var(--text-3); }
  .nav-item:hover{ background:var(--hover); color:var(--text); }
  .nav-item:hover .ico{ color:var(--text-2); }
  .nav-item.active{ background:var(--active); color:var(--text); font-weight:600; }
  .nav-item.active .ico{ color:var(--accent); }
  .nav-item .nm{ flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; text-transform:capitalize; }
  .nav-item .ct{ font-size:10.5px; font-variant-numeric:tabular-nums; color:var(--text-3);
                 background:rgba(0,0,0,.28); padding:1px 7px; border-radius:20px; flex:none; }
  .sec{ color:var(--text-3); font-size:10.5px; font-weight:600; text-transform:uppercase;
        letter-spacing:.09em; padding:18px 10px 7px; white-space:nowrap; }
  .dot{ width:6px; height:6px; border-radius:50%; display:inline-block; flex:none; }

  /* Platform chip: the service's own mark, tinted with its brand colour on a
     faint wash of the same. Reads instantly without shouting. */
  .tile{ width:24px; height:24px; border-radius:7px; display:grid; place-items:center;
         font-size:11px; font-weight:700; flex:none; }
  .tile svg{ width:14px; height:14px; display:block; }
  .tile.lg{ width:34px; height:34px; border-radius:9px; font-size:14px; }
  .tile.lg svg{ width:19px; height:19px; }

  /* ------------------------------------------------------------------ */
  /* Middle column — conversation list                                   */
  /* ------------------------------------------------------------------ */
  .list{ background:var(--panel); border-right:1px solid var(--line); display:flex; flex-direction:column; min-width:0; min-height:0; overflow:hidden; }
  .list .hd{ padding:18px 14px 12px; border-bottom:1px solid var(--line); }
  .list .hd h2{ margin:0 0 3px; font-size:15px; font-weight:650; letter-spacing:-.01em;
                text-transform:capitalize; display:flex; align-items:center; gap:10px; }
  .list .hsub{ font-size:11.5px; color:var(--text-3); padding-left:44px; margin-bottom:12px;
               font-variant-numeric:tabular-nums; min-height:14px;
               white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .searchwrap{ position:relative; }
  .searchwrap > svg{ position:absolute; left:11px; top:50%; transform:translateY(-50%); color:var(--text-3); pointer-events:none; }
  .search{ width:100%; background:var(--bg); border:1px solid var(--line-2); color:var(--text);
           border-radius:9px; padding:7px 11px 7px 32px; font-size:12.5px; outline:none;
           transition:border-color .13s ease, box-shadow .13s ease; }
  .search::placeholder{ color:var(--text-3); }
  .search:focus{ border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
  .rows{ overflow-y:auto; overflow-x:hidden; flex:1; min-height:0; padding:8px; }
  .rows-sec{ color:var(--text-3); font-size:10.5px; font-weight:600; text-transform:uppercase;
             letter-spacing:.09em; padding:12px 8px 6px; }
  .row{ display:flex; gap:11px; width:100%; padding:9px 10px; border-radius:var(--r-md);
        align-items:center; transition:background-color .13s ease; }
  .row:hover{ background:var(--hover); }
  .row.active{ background:var(--active); }
  .row.active .nm{ color:var(--text); }
  .av{ width:38px; height:38px; border-radius:50%; flex:none; display:grid; place-items:center;
       font-weight:650; font-size:14px; color:rgba(15,10,4,.72); }
  .av.sq{ border-radius:11px; background:var(--panel-2); border:1px solid var(--line-2); color:var(--text-2); }
  .av.lg{ width:34px; height:34px; font-size:13px; }
  .row .mid{ flex:1; min-width:0; display:flex; flex-direction:column; gap:1px; }
  .row .t{ display:flex; justify-content:space-between; align-items:baseline; gap:8px; }
  .row .nm{ font-weight:600; font-size:13px; color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .row .tm{ font-size:10.5px; color:var(--text-3); white-space:nowrap; font-variant-numeric:tabular-nums; flex:none; }
  .row .pv{ color:var(--text-3); font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .row .pv b{ font-weight:500; color:var(--text-2); }
  .rows .empty{ height:auto; padding:36px 16px; }

  /* ------------------------------------------------------------------ */
  /* Right pane — transcript                                             */
  /* ------------------------------------------------------------------ */
  .pane{ display:flex; flex-direction:column; min-width:0; min-height:0; overflow:hidden; background:var(--bg); }
  .pane .top{ padding:13px 22px; border-bottom:1px solid var(--line); display:flex; align-items:center;
              gap:12px; background:rgba(21,19,17,.6); flex:none; }
  .pane .top > div{ min-width:0; flex:1; }
  .pane .top .nm{ font-weight:650; font-size:14.5px; letter-spacing:-.01em;
                  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .pane .top .sub{ color:var(--text-3); font-size:11.5px; font-variant-numeric:tabular-nums; }
  .msgs{ flex:1; min-height:0; overflow-y:auto; padding:10px 22px 28px; }
  .msgstream{ max-width:840px; margin:0 auto; display:flex; flex-direction:column; }
  .daysep{ display:flex; align-items:center; gap:14px; margin:22px 0 6px; color:var(--text-3);
           font-size:11px; font-weight:600; letter-spacing:.03em; font-variant-numeric:tabular-nums; }
  .daysep::before,.daysep::after{ content:''; flex:1; height:1px; background:var(--line); }
  .msg{ display:flex; gap:10px; max-width:min(78%,600px); margin-top:12px; align-self:flex-start; }
  .msg.cont{ margin-top:2px; }
  .msg:not(.me).cont{ padding-left:38px; }
  .msg.me{ align-self:flex-end; flex-direction:row-reverse; }
  .msg .mav{ width:28px; height:28px; border-radius:50%; flex:none; align-self:flex-start; margin-top:2px;
             display:grid; place-items:center; font-size:11px; color:rgba(15,10,4,.72); font-weight:650; }
  .msg .bubble{ padding:7px 12px 5px; border-radius:16px; background:var(--bubble-them);
                border:1px solid var(--bubble-them-line); min-width:0; }
  .msg:not(.me).cont .bubble{ border-top-left-radius:6px; }
  .msg:not(.me):has(+ .msg.cont:not(.me)) .bubble{ border-bottom-left-radius:6px; }
  .msg.me .bubble{ background:var(--bubble-me); border:none; color:var(--bubble-me-text); }
  .msg.me.cont .bubble{ border-top-right-radius:6px; }
  .msg.me:has(+ .msg.me.cont) .bubble{ border-bottom-right-radius:6px; }
  .msg .who{ font-size:11px; font-weight:650; margin:1px 0 2px; }
  .msg .tx{ white-space:pre-wrap; word-break:break-word; line-height:1.45; font-size:13px; }
  .msg .tm{ font-size:10px; color:var(--text-3); margin-top:2px; text-align:right; font-variant-numeric:tabular-nums; }
  .msg.me .tm{ color:rgba(39,24,5,.55); }

  /* ------------------------------------------------------------------ */
  /* Dashboard & collection views                                        */
  /* ------------------------------------------------------------------ */
  .dash{ padding:34px 38px; overflow-y:auto; height:100%; }
  .dashin{ max-width:1060px; margin:0 auto; }
  .dhead h1{ font-size:22px; font-weight:700; letter-spacing:-.02em; margin:0 0 8px; }
  .dsub{ display:flex; align-items:center; flex-wrap:wrap; gap:10px; color:var(--text-2);
         font-size:12.5px; margin-bottom:26px; }
  .badge{ display:inline-flex; align-items:center; gap:6px; font-size:11px; font-weight:600;
          padding:3px 10px; border-radius:20px; border:1px solid; line-height:1.4; }
  .badge.good{ color:var(--good); border-color:rgba(74,222,128,.3); background:rgba(74,222,128,.08); }
  .badge.warn{ color:var(--warn); border-color:rgba(251,191,36,.3); background:rgba(251,191,36,.08); }
  code.path{ background:var(--panel); border:1px solid var(--line); padding:2px 8px; border-radius:6px; color:var(--text-2); }
  .stats{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:30px; }
  .stat{ background:var(--panel); border:1px solid var(--line); border-radius:var(--r-lg);
         padding:16px 18px 14px; position:relative; }
  .stat .ic{ position:absolute; top:14px; right:14px; color:var(--text-3); opacity:.65; }
  .stat .n{ font-size:25px; font-weight:700; letter-spacing:-.02em; font-variant-numeric:tabular-nums; }
  .stat .l{ color:var(--text-3); font-size:10.5px; font-weight:600; text-transform:uppercase;
            letter-spacing:.07em; margin-top:3px; }
  .seclabel{ color:var(--text-3); font-size:10.5px; font-weight:600; text-transform:uppercase;
             letter-spacing:.09em; margin:0 0 10px 2px; }
  .cards{ display:grid; grid-template-columns:repeat(auto-fill,minmax(215px,1fr)); gap:12px; }
  .pcard{ background:var(--panel); border:1px solid var(--line); border-radius:var(--r-lg);
          padding:15px 17px 13px; display:block; width:100%;
          transition:border-color .15s ease, transform .15s ease, box-shadow .15s ease; }
  .pcard:hover{ border-color:var(--line-2); transform:translateY(-1px); box-shadow:var(--shadow-card); }
  .pcard .h{ display:flex; align-items:center; gap:10px; }
  .pcard .pname{ font-weight:600; text-transform:capitalize; flex:1; min-width:0;
                 overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .pcard .n{ font-size:22px; font-weight:700; letter-spacing:-.02em; margin:12px 0 1px;
             font-variant-numeric:tabular-nums; }
  .pcard .s{ font-size:11.5px; color:var(--text-3); }

  .fgrid{ display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:10px; }
  .fcard{ display:flex; gap:11px; align-items:center; background:var(--panel); border:1px solid var(--line);
          border-radius:var(--r-md); padding:10px 13px; min-width:0; }
  .fcard .fmid{ min-width:0; display:flex; flex-direction:column; gap:1px; }
  .fcard .fnm{ font-weight:600; font-size:12.5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .fdt{ font-size:10.5px; color:var(--text-3); font-variant-numeric:tabular-nums; }
  .llist{ display:flex; flex-direction:column; gap:8px; }
  .listcard{ background:var(--panel); border:1px solid var(--line); border-radius:var(--r-md); padding:11px 15px; }
  .listcard .lhd{ display:flex; justify-content:space-between; gap:10px; align-items:baseline; }
  .listcard .who{ font-weight:600; font-size:12.5px; }
  .listcard .ltx{ margin-top:4px; color:var(--text-2); font-size:12.5px; white-space:pre-wrap; word-break:break-word; }

  /* ------------------------------------------------------------------ */
  /* Media — photos and video from the archive                           */
  /* ------------------------------------------------------------------ */
  .att{ display:grid; gap:4px; margin:2px 0 4px; grid-template-columns:1fr; }
  .att.n2{ grid-template-columns:1fr 1fr; }
  .att.n3,.att.n4{ grid-template-columns:1fr 1fr; }
  .att img,.att video{ width:100%; height:100%; object-fit:cover; display:block;
                       border-radius:10px; background:var(--panel-2); cursor:zoom-in; }
  .att.one img,.att.one video{ max-height:320px; width:auto; max-width:100%; object-fit:contain;
                               cursor:zoom-in; border-radius:10px; }
  .att .cell{ position:relative; aspect-ratio:1; overflow:hidden; border-radius:10px; }
  .att.one .cell{ aspect-ratio:auto; }
  .att .more{ position:absolute; inset:0; background:rgba(0,0,0,.55); color:#fff;
              display:grid; place-items:center; font-weight:650; font-size:15px; border-radius:10px; }
  .msg .bubble .att{ margin-top:4px; min-width:180px; }
  .att .broken{ display:grid; place-items:center; aspect-ratio:1; background:var(--panel-2);
                border:1px solid var(--line); border-radius:10px; color:var(--text-3); }

  .mgrid{ display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:10px; }
  .mgrid .cell{ position:relative; aspect-ratio:1; border-radius:var(--r-md); overflow:hidden;
                background:var(--panel-2); border:1px solid var(--line); }
  .mgrid img,.mgrid video{ width:100%; height:100%; object-fit:cover; display:block; cursor:zoom-in; }
  .mgrid .vbadge{ position:absolute; right:7px; bottom:7px; background:rgba(0,0,0,.6); color:#fff;
                  border-radius:5px; padding:1px 6px; font-size:10px; font-weight:600; }

  /* Lightbox */
  .lb{ position:fixed; inset:0; z-index:200; background:rgba(0,0,0,.86);
       display:grid; place-items:center; padding:48px; }
  .lb img,.lb video{ max-width:100%; max-height:100%; border-radius:10px; display:block; }
  .lb .x{ position:absolute; top:16px; right:18px; width:34px; height:34px; border-radius:50%;
          background:rgba(255,255,255,.12); color:#fff; display:grid; place-items:center; }
  .lb .x:hover{ background:rgba(255,255,255,.22); }
  .lb .nav{ position:absolute; top:50%; transform:translateY(-50%); width:40px; height:40px;
            border-radius:50%; background:rgba(255,255,255,.12); color:#fff; display:grid; place-items:center; }
  .lb .nav:hover{ background:rgba(255,255,255,.22); }
  .lb .nav.prev{ left:18px; } .lb .nav.next{ right:18px; }
  .lb .cap{ position:absolute; bottom:16px; left:0; right:0; text-align:center; color:#d4d4d8;
            font-size:12px; font-variant-numeric:tabular-nums; }

  /* ------------------------------------------------------------------ */
  /* Empty, loading & skeleton states                                    */
  /* ------------------------------------------------------------------ */
  .empty{ display:flex; flex-direction:column; align-items:center; justify-content:center; gap:5px;
          height:100%; text-align:center; padding:40px 24px; color:var(--text-2); }
  .empty .eicon{ width:46px; height:46px; border-radius:13px; background:var(--panel-2);
                 border:1px solid var(--line); display:grid; place-items:center; color:var(--text-3); margin-bottom:8px; }
  .empty h3{ margin:0; font-size:14.5px; font-weight:650; color:var(--text); letter-spacing:-.01em; }
  .empty p{ margin:0; font-size:12px; color:var(--text-3); max-width:360px; line-height:1.55; }
  .empty code{ color:var(--text-2); background:var(--panel); border:1px solid var(--line); padding:1px 6px; border-radius:5px; }
  .boot{ display:grid; place-items:center; height:100%; }
  .spinner{ width:26px; height:26px; border-radius:50%; border:3px solid var(--line-2);
            border-top-color:var(--accent); animation:spin .8s linear infinite; }
  @keyframes spin{ to{ transform:rotate(1turn); } }
  .sk{ position:relative; overflow:hidden; background:var(--panel-2); border-radius:7px; }
  .sk::after{ content:''; position:absolute; inset:0; transform:translateX(-100%);
              background:linear-gradient(90deg,transparent,rgba(255,255,255,.05),transparent);
              animation:shimmer 1.4s infinite; }
  @keyframes shimmer{ to{ transform:translateX(100%); } }
  .sk-row{ display:flex; gap:11px; padding:9px 10px; align-items:center; }
  .sk-av{ width:38px; height:38px; border-radius:50%; flex:none; }
  .sk-mid{ flex:1; display:flex; flex-direction:column; gap:7px; }
  .sk-l1{ height:10px; width:55%; }
  .sk-l2{ height:9px; width:80%; }
  .sk-bubble{ height:38px; border-radius:16px; margin-top:10px; align-self:flex-start; }
  .sk-bubble.r{ align-self:flex-end; }

  @media (prefers-reduced-motion:reduce){
    *,*::before,*::after{ animation-duration:.01ms !important; animation-iteration-count:1 !important;
                          transition-duration:.01ms !important; scroll-behavior:auto !important; }
  }

  /* ------------------------------------------------------------------ */
  /* Narrow layouts (~900-1100px): collapse the rail to icons            */
  /* ------------------------------------------------------------------ */
  @media (max-width:1100px){
    .app{ grid-template-columns:64px minmax(230px,270px) 1fr; }
    .app.mode-dash{ grid-template-columns:64px 1fr; }
    .rail{ padding:14px 9px; }
    .titlebar .tb-brand .bt{ display:none; }
    .nav-item{ justify-content:center; padding:9px 0; }
    .nav-item .nm,.nav-item .ct,.nav-item .dot{ display:none; }
    .sec{ display:none; }
    .dash{ padding:26px 24px; }
    .connect{ padding:26px 22px; }
    .msgs{ padding:10px 16px 24px; }
    .msg{ max-width:86%; }
  }
</style>
</head>
<body>
<div class="titlebar" id="titlebar">
  <div class="tb-brand" aria-label="Cold Storage"><span class="mark"><svg width="17" height="17" viewBox="0 0 24 24" aria-hidden="true"><polygon points="1.27,4.93 15.44,13.12 15.44,23.28 1.27,15.10" fill="currentColor" opacity=".55"/><polygon points="15.44,13.12 22.73,8.90 22.73,19.07 15.44,23.28" fill="currentColor" opacity=".8"/><polygon points="8.56,0.72 22.73,8.90 15.44,13.12 1.27,4.93" fill="currentColor"/></svg></span><span class="bt">Cold Storage</span></div>
  <div class="sp"></div>
  <button class="tbtn primary" id="tb-add"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14"/><path d="M5 12h14"/></svg>Connect account</button>
  <button class="tbtn icon" id="tb-menu" aria-label="More"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/></svg></button>
</div>
<div class="app mode-dash" id="app">
  <nav class="rail" id="rail" aria-label="Platforms"></nav>
  <section class="list" id="list" hidden></section>
  <main class="pane" id="pane"><div class="boot"><div class="spinner" role="status" aria-label="Loading"></div></div></main>
</div>
<div class="toasts" id="toasts" aria-live="polite"></div>
<script>
'use strict';
/* ---------- constants ---------- */
const BRAND={instagram:"#e4405f",facebook:"#1877f2",discord:"#5865f2",twitter:"#1d9bf0",
  telegram:"#229ed9",reddit:"#ff4500",whatsapp:"#25d366",google:"#4285f4",slack:"#611f69",
  snapchat:"#c9b400",linkedin:"#0a66c2"};
const AV=["#d99a84","#d9ae6b","#b3c37e","#8fcdb2","#8fb6d9","#ada0e0","#d9a0bd","#c8bd8a"];
const MONTHS=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const PATHS={
  grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  search:'<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
  shield:'<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/>',
  container:'<path d="M3.2 7.1 12 12.2v8.6L3.2 15.7z"/><path d="M12 12.2l8.8-5.1v8.6L12 20.8z"/><path d="M12 3.2l8.8 5.1L12 12.2 3.2 7.1z"/>',
  box:'<path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5"/><path d="M12 13v8"/>',
  chat:'<path d="M21 12a8 8 0 0 1-8 8H5l-2 2V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z"/>',
  users:'<path d="M17 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2"/><circle cx="10" cy="7" r="4"/><path d="M21 21v-2a4 4 0 0 0-3-3.9"/><path d="M15 3.1a4 4 0 0 1 0 7.8"/>',
  lock:'<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  unlock:'<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.7-1.4"/>',
  layers:'<path d="M12 2l9 5-9 5-9-5z"/><path d="M3 12l9 5 9-5"/><path d="M3 17l9 5 9-5"/>',
  image:'<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="M21 15l-5-5-11 11"/>',
  drive:'<rect x="2" y="7" width="20" height="10" rx="2"/><path d="M6.5 12h.01"/><path d="M10.5 12h7"/>',
  doc:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
  bookmark:'<path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>',
  plus:'<path d="M12 5v14"/><path d="M5 12h14"/>',
  external:'<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6"/><path d="M10 14 21 3"/>',
  folder:'<path d="M3 7a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  key:'<circle cx="7.5" cy="15.5" r="4.5"/><path d="M10.7 12.3 21 2"/><path d="M16 7l3 3"/><path d="M18 5l3 3"/>',
  check:'<path d="M20 6 9 17l-5-5"/>',
  /* lucide: refresh-cw, clock, alert-circle, plug-zap, download-cloud, settings */
  refresh:'<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  alert:'<circle cx="12" cy="12" r="9"/><path d="M12 8v5"/><path d="M12 16h.01"/>',
  plug:'<path d="M6.3 20.7a2.4 2.4 0 0 1 0-3.4l3-3a2.4 2.4 0 0 1 3.4 0l1 1a2.4 2.4 0 0 1 0 3.4l-3 3a2.4 2.4 0 0 1-3.4 0z"/><path d="m14 8 2-2"/><path d="M17.7 3.3a2.4 2.4 0 0 1 3.4 3.4l-3 3a2.4 2.4 0 0 1-3.4 0l-1-1a2.4 2.4 0 0 1 0-3.4z"/><path d="m8 14-2 2"/>',
  cloud:'<path d="M12 13v8"/><path d="m8 17 4 4 4-4"/><path d="M20.9 18.4A5 5 0 0 0 18 9h-1.3A8 8 0 1 0 4 16.2"/>',
  chevL:'<path d="m15 18-6-6 6-6"/>',
  chevR:'<path d="m9 18 6-6-6-6"/>',
  cog:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 7 19.4a1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9H1a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 2.6 7a1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H7a1.7 1.7 0 0 0 1-1.5V1a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V7a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'
};
/* ---------- tiny helpers ---------- */
function ic(n,s,w){s=s||16;w=w||2;return `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${PATHS[n]||''}</svg>`;}
/* The product mark: an isometric shipping container, drawn from three flat
   faces so it tints with currentColor and stays legible at 14px. */
function brandMark(s){s=s||15;return `<svg width="${s}" height="${s}" viewBox="0 0 24 24" aria-hidden="true"><polygon points="1.27,4.93 15.44,13.12 15.44,23.28 1.27,15.10" fill="currentColor" opacity=".55"/><polygon points="15.44,13.12 22.73,8.90 22.73,19.07 15.44,23.28" fill="currentColor" opacity=".8"/><polygon points="8.56,0.72 22.73,8.90 15.44,13.12 1.27,4.93" fill="currentColor"/>`;}
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
function escA(s){return esc(s).replace(/"/g,'&quot;');}
function brandColor(id){return BRAND[id]||"#71717a";}
/* Each platform's own mark, drawn inline so the UI still works with no network.
   `s` means stroke the path instead of filling it. */
const PLATFORM_ICONS={
  instagram:{s:1,p:'<rect x="2.6" y="2.6" width="18.8" height="18.8" rx="5.4"/><circle cx="12" cy="12" r="4.6"/><circle cx="17.5" cy="6.5" r="1.15" fill="currentColor" stroke="none"/>'},
  facebook:{p:'M22 12a10 10 0 1 0-11.56 9.88v-6.99H7.9V12h2.54V9.8c0-2.5 1.49-3.89 3.77-3.89 1.09 0 2.24.2 2.24.2v2.46h-1.26c-1.24 0-1.63.77-1.63 1.56V12h2.78l-.45 2.89h-2.33v6.99A10 10 0 0 0 22 12z'},
  twitter:{p:'M17.53 3h3.02l-6.6 7.54L21.75 21h-6.07l-4.76-6.22L5.47 21H2.45l7.06-8.07L2.25 3h6.22l4.3 5.69L17.53 3zm-1.06 16.2h1.67L7.63 4.7H5.84l10.63 14.5z'},
  whatsapp:{p:'M12.04 2C6.58 2 2.13 6.45 2.13 11.91c0 1.75.46 3.45 1.32 4.95L2 22l5.25-1.38a9.87 9.87 0 0 0 4.79 1.22c5.46 0 9.9-4.45 9.9-9.91C21.95 6.45 17.5 2 12.04 2zm5.8 14.17c-.25.69-1.45 1.32-1.99 1.4-.51.08-1.15.11-1.86-.12a16.9 16.9 0 0 1-1.68-.62c-2.96-1.28-4.89-4.26-5.04-4.46-.15-.2-1.2-1.6-1.2-3.05 0-1.45.76-2.16 1.03-2.46.27-.3.59-.37.79-.37h.57c.18 0 .43-.07.67.51.25.6.85 2.05.92 2.2.08.15.13.32.03.52-.1.2-.15.32-.3.5l-.44.51c-.15.15-.3.31-.13.61.17.3.76 1.25 1.63 2.03 1.12 1 2.06 1.31 2.36 1.46.3.15.47.13.64-.08.17-.2.74-.86.94-1.16.2-.3.4-.25.67-.15.27.1 1.72.81 2.01.96.3.15.5.22.57.35.07.12.07.72-.18 1.42z'},
  telegram:{p:'M11.94 2C6.46 2 2 6.46 2 11.94s4.46 9.94 9.94 9.94 9.94-4.46 9.94-9.94S17.42 2 11.94 2zm4.6 6.77-1.54 7.26c-.11.51-.42.63-.85.39l-2.35-1.73-1.13 1.09c-.13.13-.24.24-.48.24l.17-2.4 4.37-3.95c.19-.17-.04-.26-.29-.09L9.04 13.3l-2.33-.73c-.5-.16-.51-.5.11-.75l9.1-3.51c.42-.15.79.1.65.75z'},
  discord:{p:'M20.32 4.37A19.8 19.8 0 0 0 15.43 3c-.24.42-.46.86-.63 1.28a18.3 18.3 0 0 0-5.6 0C9.03 3.86 8.81 3.42 8.57 3a19.7 19.7 0 0 0-4.89 1.37C.58 8.98-.26 13.48.16 17.9a19.9 19.9 0 0 0 6.03 3.06c.49-.66.92-1.37 1.29-2.11a12.9 12.9 0 0 1-2.03-.98c.17-.13.34-.26.5-.4a14.2 14.2 0 0 0 12.1 0c.16.14.33.28.5.4-.65.39-1.33.72-2.04.98.37.74.8 1.45 1.29 2.11a19.9 19.9 0 0 0 6.04-3.06c.5-5.12-.84-9.58-3.52-13.53zM8.02 15.2c-1.18 0-2.16-1.08-2.16-2.4 0-1.33.95-2.42 2.16-2.42s2.18 1.09 2.16 2.42c0 1.32-.96 2.4-2.16 2.4zm7.96 0c-1.18 0-2.16-1.08-2.16-2.4 0-1.33.95-2.42 2.16-2.42s2.18 1.09 2.16 2.42c0 1.32-.95 2.4-2.16 2.4z'},
  reddit:{p:'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm5.8 11.33c.02.16.03.32.03.48 0 2.46-2.86 4.45-6.4 4.45s-6.4-1.99-6.4-4.45c0-.17.01-.33.03-.49a1.6 1.6 0 1 1 1.8-2.57 7.86 7.86 0 0 1 4.25-1.34l.81-3.8a.33.33 0 0 1 .39-.26l2.67.57a1.15 1.15 0 1 1-.15.72l-2.32-.5-.72 3.4a7.85 7.85 0 0 1 4.16 1.34 1.6 1.6 0 1 1 1.85 2.55zM9 13.5a1.15 1.15 0 1 1 2.3 0 1.15 1.15 0 0 1-2.3 0zm5.75 1.15a1.15 1.15 0 1 1 0-2.3 1.15 1.15 0 0 1 0 2.3zm.2 1.83c.14.14.14.36 0 .5-.88.88-2.57.95-3.06.95s-2.18-.07-3.06-.95a.35.35 0 0 1 .5-.5c.55.56 1.75.76 2.56.76s2-.2 2.56-.76c.14-.14.36-.14.5 0z'},
  google:{p:'M21.35 11.1H12v3.2h5.35c-.23 1.4-1.66 4.1-5.35 4.1-3.22 0-5.85-2.67-5.85-5.95S8.78 6.5 12 6.5c1.83 0 3.06.78 3.76 1.45l2.56-2.47C16.68 3.96 14.53 3 12 3 6.98 3 3 6.98 3 12s3.98 9 9 9c5.2 0 8.65-3.65 8.65-8.8 0-.59-.06-1.04-.3-2.1z'},
  snapchat:{p:'M12 2.2c2.83 0 4.98 2.15 5.08 4.98v2.25c.49.2.98-.2 1.47-.2.49 0 .98.29.98.78 0 .69-1.08.98-1.76 1.27-.29.1-.49.29-.39.59.49 1.57 1.96 3.04 3.53 3.33.29.1.49.29.39.59-.2.59-1.37.88-2.25.98-.2 0-.29.2-.39.39-.1.39-.2.88-.49.98-.39.1-.98-.2-1.76-.2-1.08 0-1.57.2-2.35.78-.69.49-1.27.98-2.35.98s-1.66-.49-2.35-.98c-.78-.59-1.27-.78-2.35-.78-.78 0-1.37.29-1.76.2-.29-.1-.39-.59-.49-.98-.1-.2-.2-.39-.39-.39-.88-.1-2.06-.39-2.25-.98-.1-.29.1-.49.39-.59 1.57-.29 3.04-1.76 3.53-3.33.1-.29-.1-.49-.39-.59-.69-.29-1.76-.59-1.76-1.27 0-.49.49-.78.98-.78.49 0 .98.39 1.47.2V7.18C7.02 4.35 9.17 2.2 12 2.2z'},
  linkedin:{p:'M20.45 20.45h-3.56v-5.57c0-1.33-.03-3.04-1.85-3.04-1.86 0-2.14 1.45-2.14 2.94v5.67H9.35V9h3.41v1.56h.05c.48-.9 1.63-1.85 3.37-1.85 3.6 0 4.27 2.37 4.27 5.46v6.28zM5.34 7.43a2.07 2.07 0 1 1 0-4.14 2.07 2.07 0 0 1 0 4.14zm1.78 13.02H3.55V9h3.57v11.45zM22.22 0H1.77C.79 0 0 .77 0 1.72v20.56C0 23.23.79 24 1.77 24h20.45c.98 0 1.78-.77 1.78-1.72V1.72C24 .77 23.2 0 22.22 0z'},
  slack:{p:'M5.04 15.17a2.53 2.53 0 1 1-2.52-2.52h2.52v2.52zm1.27 0a2.53 2.53 0 0 1 5.05 0v6.31a2.53 2.53 0 0 1-5.05 0v-6.31zM8.83 5.04a2.53 2.53 0 1 1 2.53-2.52v2.52H8.83zm0 1.27a2.53 2.53 0 0 1 0 5.05H2.52a2.53 2.53 0 0 1 0-5.05h6.31zM18.96 8.83a2.53 2.53 0 1 1 2.52 2.52h-2.52V8.83zm-1.27 0a2.53 2.53 0 0 1-5.04 0V2.52a2.53 2.53 0 0 1 5.04 0v6.31zM15.17 18.96a2.53 2.53 0 1 1-2.52 2.52v-2.52h2.52zm0-1.27a2.53 2.53 0 0 1 0-5.04h6.31a2.53 2.53 0 0 1 0 5.04h-6.31z'}
};
function tile(id,cls){
  const g=PLATFORM_ICONS[id], col=brandColor(id);
  if(!g) return `<span class="tile ${cls||''}" style="color:${col}">${esc((id[0]||'?').toUpperCase())}</span>`;
  const inner=g.s
    ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${g.p}</svg>`
    : `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="${g.p}"/></svg>`;
  return `<span class="tile ${cls||''}" style="color:${col};background:${col}1a">${inner}</span>`;
}
function avColor(s){let h=0;for(const c of (s||'?'))h=(h*31+c.charCodeAt(0))>>>0;return AV[h%AV.length];}
function initial(s){return esc((s||'?').trim()[0]?.toUpperCase()||'?');}
/* Timestamps are archive data like any other: a platform can hand back
   anything, and these all land in innerHTML. Unescaped, one malformed value
   swallowed every conversation after it in the list. */
function fmtTime(s){return esc((s||'').slice(11,16));}
function dayKey(s){return (s||'').slice(0,10);}
function dayLabel(k){if(!k)return'';const p=k.split('-');const mi=+p[1]-1;
  if(!(mi>=0&&mi<12)||!/^\d{4}$/.test(p[0])||isNaN(+p[2]))return esc(k);
  return MONTHS[mi]+' '+(+p[2])+', '+p[0];}
function fmtShort(s){const k=dayKey(s);if(!k)return'';const p=k.split('-');const mi=+p[1]-1;
  if(!(mi>=0&&mi<12)||!/^\d{4}$/.test(p[0])||isNaN(+p[2]))return esc(k);
  return +p[0]===new Date().getFullYear()?MONTHS[mi]+' '+(+p[2]):MONTHS[mi]+' '+p[0];}
function fmtCompact(n){if(n>=1e6)return (n/1e6).toFixed(1).replace(/\.0$/,'')+'M';
  if(n>=1e4)return Math.round(n/1e3)+'k';if(n>=1e3)return (n/1e3).toFixed(1).replace(/\.0$/,'')+'k';
  return String(n);}
function emptyState(icon,title,html){return `<div class="empty"><span class="eicon">${ic(icon,20,1.8)}</span><h3>${title}</h3><p>${html}</p></div>`;}

/* ---------- media ---------- */
/* Records carry `media` as a JSON array of sha256 content hashes; each is
   served decrypted from /media/<sha>. Video is detected on load rather than by
   filename, since the archive stores hashes and not names. */
const VIDEO_RE=/\.(mp4|mov|webm|m4v)$/i;
function mediaList(rec){
  const raw=rec&&rec.media; if(!raw) return [];
  let arr=raw;
  if(typeof raw==='string'){ try{ arr=JSON.parse(raw); }catch(e){ return []; } }
  if(!Array.isArray(arr)) return [];
  return arr.filter(s=>typeof s==='string'&&/^[0-9a-f]{64}$/i.test(s));
}
function mediaUrl(sha){ return '/media/'+encodeURIComponent(sha); }
/* Everything currently open in the lightbox, so arrow keys can page through. */
let LB=[], LBI=0;
function mediaCell(sha,cls){
  return `<span class="cell"><img loading="lazy" decoding="async" src="${escA(mediaUrl(sha))}" alt=""`
    +` data-lb="${escA(sha)}"${cls?` class="${escA(cls)}"`:''}></span>`;
}
/* A blob that won't decode (wrong key, truncated) becomes a placeholder rather
   than a browser's broken-image glyph. Delegated in the capture phase because
   'error' does not bubble. */
document.addEventListener('error',(e)=>{
  const img=e.target;
  if(!(img instanceof HTMLImageElement)||!img.dataset.lb) return;
  const cell=img.closest('.cell'); if(!cell) return;
  cell.innerHTML=`<span class="broken">${ic('image',18,1.7)}</span>`;
},true);
/* Attachments under a chat bubble: up to 4 tiles, the last one counting the rest. */
function attachments(shas){
  if(!shas.length) return '';
  const shown=shas.slice(0,4), extra=shas.length-shown.length;
  const cls=shown.length===1?'att one':'att n'+shown.length;
  return `<span class="${cls}" data-group="${escA(shas.join(','))}">`
    +shown.map((s,i)=>{
      const cell=mediaCell(s);
      if(i===3&&extra>0) return cell.replace('</span>',`<span class="more">+${extra}</span></span>`);
      return cell;
    }).join('')+`</span>`;
}
function openLightbox(group,sha){
  LB=group.filter(Boolean); LBI=Math.max(0,LB.indexOf(sha));
  let el=document.getElementById('lightbox');
  if(!el){
    el=document.createElement('div'); el.className='lb'; el.id='lightbox';
    el.setAttribute('role','dialog'); el.setAttribute('aria-modal','true');
    el.setAttribute('aria-label','Photo viewer'); el.tabIndex=-1;
    el.addEventListener('click',(e)=>{ if(e.target===el||e.target.closest('.x')) closeLightbox(); });
    document.body.appendChild(el);
    document.addEventListener('keydown',lbKeys);
  }
  renderLightbox();
}
function renderLightbox(){
  const el=document.getElementById('lightbox'); if(!el) return;
  const sha=LB[LBI]; if(!sha) return closeLightbox();
  const many=LB.length>1;
  el.innerHTML=`<button class="x" aria-label="Close">${ic('plus',18,2.2)}</button>`
    +(many?`<button class="nav prev" aria-label="Previous">${ic('chevL',20,2.2)}</button>`:'')
    +`<img src="${escA(mediaUrl(sha))}" alt="">`
    +(many?`<button class="nav next" aria-label="Next">${ic('chevR',20,2.2)}</button>`:'')
    +(many?`<div class="cap">${LBI+1} of ${LB.length}</div>`:'');
  el.querySelector('.x').style.transform='rotate(45deg)'; /* reuse the + glyph as a close X */
  el.focus(); /* so Escape and the arrow keys reach the dialog, not the page behind it */
  const p=el.querySelector('.prev'), n=el.querySelector('.next');
  if(p) p.addEventListener('click',(e)=>{ e.stopPropagation(); step(-1); });
  if(n) n.addEventListener('click',(e)=>{ e.stopPropagation(); step(1); });
}
function step(d){ if(!LB.length) return; LBI=(LBI+d+LB.length)%LB.length; renderLightbox(); }
function closeLightbox(){
  const el=document.getElementById('lightbox'); if(el) el.remove();
  document.removeEventListener('keydown',lbKeys); LB=[]; LBI=0;
}
function lbKeys(e){
  if(e.key==='Escape') closeLightbox();
  else if(e.key==='ArrowRight') step(1);
  else if(e.key==='ArrowLeft') step(-1);
}
/* One delegated listener for the whole page — survives every re-render. */
document.addEventListener('click',(e)=>{
  const img=e.target.closest('[data-lb]'); if(!img) return;
  const holder=img.closest('[data-group]');
  const group=holder?(holder.dataset.group||'').split(',').filter(Boolean):[img.dataset.lb];
  openLightbox(group,img.dataset.lb);
});
function skRows(n){let h='';for(let i=0;i<n;i++)h+=`<div class="sk-row"><div class="sk sk-av"></div><div class="sk-mid"><div class="sk sk-l1" style="width:${45+((i*13)%30)}%"></div><div class="sk sk-l2" style="width:${65+((i*17)%25)}%"></div></div></div>`;return h;}
function skMsgs(){let h='';const w=[46,58,34,52,40,62];for(let i=0;i<6;i++)h+=`<div class="sk sk-bubble ${i%3===2?'r':''}" style="width:${w[i]}%"></div>`;return h;}

/* ---------- state ---------- */
let STATUS=null, active=null, activeThread=null, activeSpecial=null, SELF=null;
let THREADS=[], SPECIALS=[], VIS=[];
let onConnect=false;
/* Every async view bumps this before fetching and re-checks it before painting.
   Guarding on the thread NAME alone let a slow Instagram fetch paint into a
   Reddit view when both had a thread called "Mom". */
let REQ=0;

/* ---------- native bridge (present only inside the desktop app) ---------- */
const BRIDGE=(typeof window!=='undefined' && window.coldBridge) ? window.coldBridge : null;

/* Accounts state, pushed from the native side. In a plain browser this stays
   empty and the UI falls back to "add a downloaded export by hand". */
let ACCOUNTS=[], PREFS={launchAtLogin:false};
const SCHEDULES=[['daily','Every day'],['weekly','Every week'],['monthly','Every month'],['manual','Only when I ask']];

/* ---------- native actions + toasts ---------- */
function openExternal(url){
  if(!url) return;
  if(BRIDGE&&BRIDGE.openExternal) BRIDGE.openExternal(url);
  else window.open(url,'_blank','noopener');
}
function addExport(){
  if(BRIDGE&&BRIDGE.addExport){ BRIDGE.addExport(); }
  else toast('work','Add from the terminal','Run <code>cold ingest ~/Downloads/your-export.zip</code>, then this page updates automatically.',{ms:7000});
}
let TSEQ=0, ingestToast=null;
function toastMarkup(kind,title,body){
  const icon = kind==='work' ? '<span class="tsp"></span>'
    : ic(kind==='good'?'check':(kind==='bad'?'box':'box'),15,2.2);
  return `<span class="ti">${icon}</span><span class="tt"><b>${esc(title)}</b>${body?'<br>'+body:''}</span>`;
}
function toast(kind,title,body,opts){
  opts=opts||{};
  const wrap=document.getElementById('toasts'); if(!wrap) return null;
  const id='t'+(++TSEQ);
  const el=document.createElement('div'); el.className='toast '+kind; el.id=id;
  el.innerHTML=toastMarkup(kind,title,body); wrap.appendChild(el);
  if(!opts.sticky) setTimeout(()=>dismissToast(id),opts.ms||4200);
  return id;
}
function updateToast(id,kind,title,body,opts){
  opts=opts||{};
  const el=id&&document.getElementById(id);
  if(!el) return toast(kind,title,body,opts);
  el.className='toast '+kind; el.innerHTML=toastMarkup(kind,title,body);
  if(!opts.sticky) setTimeout(()=>dismissToast(id),opts.ms||4200);
  return id;
}
function dismissToast(id){
  const el=document.getElementById(id); if(!el) return;
  el.style.transition='opacity .3s ease, transform .3s ease'; el.style.opacity='0'; el.style.transform='translateY(6px)';
  setTimeout(()=>el.remove(),300);
}
function handleIngest(p){
  if(!p) return;
  if(p.phase==='start'){
    ingestToast=toast('work','Backing up…','Reading your export and encrypting it on this Mac.',{sticky:true});
  }else if(p.phase==='done'){
    updateToast(ingestToast,'good','Backup complete',esc(p.summary||'Done.'),{ms:5000});
    ingestToast=null; refreshData();
  }else if(p.phase==='retry'){
    updateToast(ingestToast,'work','Retrying…',esc((p.error||'').split('\n').filter(Boolean).pop()||''),{sticky:true});
  }else if(p.phase==='error'){
    const last=(p.error||'').split('\n').filter(Boolean).pop()||'Something went wrong.';
    // A file we can never read is not a failure to retry — it's an instruction.
    if(p.permanent) updateToast(ingestToast,'bad','Can’t read '+(p.name||'that file'),esc(last),{ms:14000});
    else updateToast(ingestToast,'bad','Backup failed',esc(last),{ms:9000});
    ingestToast=null;
  }
}
async function refreshData(){
  try{ STATUS=await (await fetch('/api/status')).json(); }catch(e){ return; }
  renderRail();
  // A scheduled backup can finish at any moment. Rebuilding the view would
  // dump the user back at the top of the first conversation mid-read, so the
  // counts refresh and the place they were keeps itself.
  if(onConnect){ showConnect(); return; }
  if(!active){ showDashboard(); return; }
  const wasThread=activeThread, wasSpecial=activeSpecial, q=curQuery();
  await openConnector(active);
  const box=document.getElementById('q'); if(box&&q){ box.value=q; filterThreads(); }
  if(wasThread&&THREADS.some(t=>t.thread===wasThread)) openThread(wasThread);
  else if(wasSpecial){ const sp=SPECIALS.find(s=>s.ty===wasSpecial); if(sp) openSpecial(sp); }
}

/* ---------- top-bar menu ---------- */
function toggleMenu(anchor){
  const ex=document.getElementById('appmenu'); if(ex){ ex.remove(); return; }
  const m=document.createElement('div'); m.className='menu'; m.id='appmenu';
  m.setAttribute('role','menu');
  let items=`<button data-mi="add"><span class="ico">${ic('plus',15,2)}</span>Add a downloaded export</button>`;
  if(BRIDGE){
    items+=`<button data-mi="syncall"><span class="ico">${ic('refresh',15,1.9)}</span>Back up all accounts now</button>`
      +`<div class="sep"></div>`
      +`<button data-mi="login"><span class="ico">${ic('clock',15,1.8)}</span><span style="flex:1">Start at login</span><span class="sw${PREFS.launchAtLogin?' on':''}" id="sw-login"></span></button>`
      +`<div class="sep"></div>`
      +`<button data-mi="reveal"><span class="ico">${ic('folder',15,1.9)}</span>Reveal data folder</button>`
      +`<button data-mi="kit"><span class="ico">${ic('key',15,1.8)}</span>Show Recovery Kit</button>`;
  }
  m.innerHTML=items; document.body.appendChild(m);
  const r=anchor.getBoundingClientRect();
  m.style.top=(r.bottom+6)+'px'; m.style.right=Math.max(8,(window.innerWidth-r.right))+'px';
  m.querySelectorAll('[data-mi]').forEach(el=>el.addEventListener('click',(ev)=>{
    const a=el.dataset.mi;
    if(a==='login'&&BRIDGE){ // toggle in place; keep the menu open
      ev.stopPropagation();
      PREFS.launchAtLogin=!PREFS.launchAtLogin;
      BRIDGE.setPref('launchAtLogin',PREFS.launchAtLogin);
      const sw=document.getElementById('sw-login'); if(sw) sw.classList.toggle('on',PREFS.launchAtLogin);
      return;
    }
    m.remove();
    if(a==='add') addExport();
    else if(a==='syncall'&&BRIDGE){ BRIDGE.syncAll(); toast('work','Backing up all accounts','Checking each connected platform for a ready export.',{ms:5000}); }
    else if(a==='reveal'&&BRIDGE) BRIDGE.revealDataFolder();
    else if(a==='kit'&&BRIDGE) BRIDGE.showRecoveryKit();
  }));
  const onKey=(e)=>{ if(e.key==='Escape'){ m.remove(); document.removeEventListener('keydown',onKey); anchor.focus(); } };
  document.addEventListener('keydown',onKey);
  setTimeout(()=>document.addEventListener('click',function h(e){
    if(!m.contains(e.target)&&e.target!==anchor){
      m.remove(); document.removeEventListener('click',h); document.removeEventListener('keydown',onKey);
    }
  },{once:false}),0);
}

/* ---------- boot ---------- */
async function boot(){
  if(BRIDGE) document.body.classList.add('electron');
  const add=document.getElementById('tb-add'); if(add) add.addEventListener('click',showConnect);
  const mn=document.getElementById('tb-menu'); if(mn) mn.addEventListener('click',(e)=>{ e.stopPropagation(); toggleMenu(mn); });
  if(BRIDGE&&BRIDGE.onIngest) BRIDGE.onIngest(handleIngest);
  if(BRIDGE&&BRIDGE.onAccounts) BRIDGE.onAccounts(list=>{ ACCOUNTS=list; if(onConnect) showConnect(); });
  const pane=document.getElementById('pane');
  try{ STATUS=await (await fetch('/api/status')).json(); }
  catch(e){ pane.innerHTML=emptyState('box','Could not load the archive','The local server did not respond. Close this tab and run <code>cold open</code> again.'); return; }
  if(BRIDGE&&BRIDGE.accounts){ try{ ACCOUNTS=await BRIDGE.accounts(); }catch(e){} }
  if(BRIDGE&&BRIDGE.getPrefs){ try{ PREFS=await BRIDGE.getPrefs()||PREFS; }catch(e){} }
  renderRail(); showDashboard();
}

/* ---------- left rail ---------- */
function renderRail(){
  const r=document.getElementById('rail');
  let h=`<button class="nav-item ${(!active&&!onConnect)?'active':''}" data-nav="dash" title="Overview"><span class="ico">${ic('grid',16,1.8)}</span><span class="nm">Overview</span></button>`;
  const needs=ACCOUNTS.filter(a=>a.attention||a.lastResult==='attention'||a.lastResult==='reconnect').length;
  h+=`<button class="nav-item ${onConnect?'active':''}" data-nav="connect" title="Accounts"><span class="ico">${ic('plug',16,1.9)}</span><span class="nm">Accounts</span>`
    +(needs?`<span class="dot" style="background:var(--warn)"></span>`:'')+`</button>`;
  if(STATUS.connectors.length) h+=`<div class="sec">Platforms</div>`;
  STATUS.connectors.forEach((c,i)=>{
    const col=c.last_status==='error'?'var(--bad)':(c.stale?'var(--warn)':'var(--good)');
    h+=`<button class="nav-item ${active===c.connector?'active':''}" data-nav="conn" data-i="${i}" title="${escA(c.connector)}" aria-current="${active===c.connector?'page':'false'}">`
      +`<span class="ico">${tile(c.connector)}</span><span class="nm">${esc(c.connector)}</span>`
      +`<span class="dot" style="background:${col}"></span><span class="ct">${fmtCompact(c.records)}</span></button>`;
  });
  r.innerHTML=h;
  r.querySelectorAll('[data-nav]').forEach(el=>{
    el.addEventListener('click',()=>{
      const n=el.dataset.nav;
      if(n==='dash') showDashboard();
      else if(n==='connect') showConnect();
      else openConnector(STATUS.connectors[+el.dataset.i].connector);
    });
  });
}

/* ---------- accounts view ---------- */
function relTime(iso){
  if(!iso) return null;
  const d=Date.parse(iso); if(!d) return null;
  const s=Math.max(0,(Date.now()-d)/1000);
  if(s<90) return 'just now';
  if(s<5400) return Math.round(s/60)+'m ago';
  if(s<172800) return Math.round(s/3600)+'h ago';
  return Math.round(s/86400)+'d ago';
}
/* One account's state, as a pill + a sentence. */
function acctState(a){
  if(a.busy) return {cls:'',pulse:true,label:'Working',line:'Checking with '+a.name+'…'};
  if(a.attention||a.lastResult==='attention')
    return {cls:'wait',label:'Needs a click',line:a.detail||'One step needs you — press Finish.'};
  if(a.lastResult==='reconnect')
    return {cls:'err',label:'Reconnect',line:a.detail||'The session expired. Connect again.'};
  if(a.lastResult==='error')
    return {cls:'err',label:'Retrying',line:(a.detail||'The last backup did not finish.')+' We keep trying on our own.'};
  if(a.lastResult==='downloading')
    return {cls:'',pulse:true,label:'Downloading',line:a.detail||'Downloading your export…'};
  if(a.lastResult==='ingesting')
    return {cls:'',pulse:true,label:'Backing up',line:a.detail||'Adding it to your encrypted archive…'};
  if(a.lastResult==='requested')
    return {cls:'wait',label:'Preparing',line:(a.detail||a.name+' is preparing your export')+' — we check back on our own.'};
  if(a.lastSuccess)
    return {cls:'ok',label:'Backed up',line:'Last backup '+(relTime(a.lastSuccess)||'recently')+'.'};
  return {cls:'ok',label:'Connected',line:'First backup starts automatically.'};
}
function schedSelect(a){
  const opts=SCHEDULES.map(([v,l])=>`<option value="${v}"${a.schedule===v?' selected':''}>${l}</option>`).join('');
  return `<select class="sel" data-sched="${escA(a.id)}" aria-label="Backup frequency for ${escA(a.name)}">${opts}</select>`;
}
function showConnect(){
  onConnect=true; active=null; activeThread=null; activeSpecial=null; renderRail();
  document.getElementById('list').hidden=true;
  document.getElementById('app').classList.add('mode-dash');
  const auto=ACCOUNTS.filter(a=>a.auto), manual=ACCOUNTS.filter(a=>!a.auto);
  const connectedCount=auto.filter(a=>a.connected).length;

  let rows;
  if(!BRIDGE){
    rows=`<div class="card"><div class="manual"><div class="mm">
      <div class="mn">Automatic backups need the desktop app</div>
      <div class="mr">You’re viewing the archive in a browser. Open the Cold Storage app to connect accounts and run backups on a schedule.</div>
    </div></div></div>`;
  }else{
    rows=`<div class="card">`+auto.map(a=>{
      const st=acctState(a);
      const pill=`<span class="pill ${st.cls}"><span class="lamp${st.pulse?' pulse':''}"></span>${esc(st.label)}</span>`;
      // A expired session cannot be fixed by retrying the sync — it needs a
      // fresh sign-in, so route that state to Connect rather than Back up now.
      const needsLogin = a.lastResult==='reconnect';
      const acts=a.connected
        ? `${schedSelect(a)}
           ${needsLogin
             ? `<button class="pbtn solid" data-conn="${escA(a.id)}">${ic('plug',12,2.2)} Reconnect</button>`
             : (a.attention||a.lastResult==='attention')
               ? `<button class="pbtn solid" data-sync="${escA(a.id)}">Finish</button>`
               : `<button class="pbtn" data-sync="${escA(a.id)}"${a.busy?' disabled':''}>${a.busy?'<span class="spin"></span>':ic('refresh',12,2.2)} Back up now</button>`}
           <button class="pbtn ghost" data-disc="${escA(a.id)}" title="Disconnect ${escA(a.name)}">Disconnect</button>`
        : `<button class="pbtn solid" data-conn="${escA(a.id)}">${ic('plug',12,2.2)} Connect</button>`;
      return `<div class="acct">${tile(a.id,'lg')}
        <div class="amid"><div class="an">${esc(a.name)}</div>
          <div class="as">${a.connected?pill+'<span>'+esc(st.line)+'</span>':'<span>Sign in once — backups run on their own after that.</span>'}</div></div>
        <div class="aacts">${acts}</div></div>`;
    }).join('')+`</div>`;
  }

  const manualRows=manual.length?`<div class="seclabel" style="margin-top:26px">Add by hand</div>
    <div class="card">`+manual.map(a=>`<div class="manual">${tile(a.id,'lg')}
      <div class="mm"><div class="mn">${esc(a.name)}</div><div class="mr">${esc(a.manualReason||'')}</div></div>
      <button class="pbtn" data-add="1">${ic('plus',12,2.4)} Add file</button></div>`).join('')+`</div>`:'';

  const addRow = BRIDGE
    ? `<button class="card addrow" id="cn-add" style="width:100%;margin-top:12px">
         <span class="ab-ic">${ic('cloud',18,1.9)}</span>
         <span><span class="ab-t">Add a downloaded export</span>
         <span class="ab-s">Anything you download yourself — the platform is detected automatically. Exports that land in your Downloads folder are picked up on their own.</span></span></button>`
    : `<div class="card addrow" style="margin-top:12px"><span class="ab-ic">${ic('cloud',18,1.9)}</span>
         <span><span class="ab-t">Add a downloaded export</span>
         <span class="ab-s">In the terminal: <code>cold ingest ~/Downloads/your-export.zip</code></span></span></div>`;

  const sub = BRIDGE
    ? (connectedCount
        ? connectedCount+(connectedCount===1?' account connected':' accounts connected')+' — backups run in the background, even when this window is closed.'
        : 'Connect an account once. From then on this app requests your official export, downloads it, and files it away on the schedule you pick. Nothing is ever uploaded.')
    : 'Everything stays on this Mac. Nothing is ever uploaded.';

  document.getElementById('pane').innerHTML=`<div class="connect"><div class="connectin">
    <header class="dhead"><h1>Accounts</h1><div class="dsub"><span>${esc(sub)}</span></div></header>
    <div class="seclabel">Automatic</div>
    ${rows}
    ${addRow}
    ${manualRows}</div></div>`;

  const pane=document.getElementById('pane');
  const ab=document.getElementById('cn-add'); if(ab) ab.addEventListener('click',addExport);
  pane.querySelectorAll('[data-add]').forEach(el=>el.addEventListener('click',addExport));
  pane.querySelectorAll('[data-conn]').forEach(el=>el.addEventListener('click',async()=>{
    el.disabled=true; el.innerHTML='<span class="spin"></span> Waiting for sign-in…';
    await BRIDGE.connect(el.dataset.conn); await loadAccounts();
  }));
  pane.querySelectorAll('[data-sync]').forEach(el=>el.addEventListener('click',()=>{
    BRIDGE.syncNow(el.dataset.sync); el.disabled=true; el.innerHTML='<span class="spin"></span> Working…';
  }));
  pane.querySelectorAll('[data-disc]').forEach(el=>el.addEventListener('click',async()=>{
    await BRIDGE.disconnect(el.dataset.disc); await loadAccounts();
  }));
  pane.querySelectorAll('[data-sched]').forEach(el=>el.addEventListener('change',()=>{
    BRIDGE.setSchedule(el.dataset.sched,el.value);
    toast('good','Schedule updated','Backups for '+esc(el.closest('.acct').querySelector('.an').textContent)+' now run '+esc(el.options[el.selectedIndex].text.toLowerCase())+'.');
  }));
}
async function loadAccounts(){
  if(!BRIDGE||!BRIDGE.accounts) return;
  try{ ACCOUNTS=await BRIDGE.accounts(); }catch(e){ return; }
  if(onConnect) showConnect();
  renderRail();
}

/* ---------- dashboard ---------- */
function statTile(n,label,icon){
  return `<div class="stat"><span class="ic">${ic(icon,16,1.7)}</span><div class="n">${n}</div><div class="l">${label}</div></div>`;
}
function showDashboard(){
  active=null; activeThread=null; activeSpecial=null; onConnect=false; renderRail();
  document.getElementById('list').hidden=true;
  document.getElementById('app').classList.add('mode-dash');
  const s=STATUS, pane=document.getElementById('pane');
  const enc=s.encrypted
    ?`<span class="badge good">${ic('lock',11,2.2)}Encrypted</span>`
    :`<span class="badge warn">${ic('unlock',11,2.2)}Not encrypted</span>`;
  // Freshest backup across every account: the one number that answers
  // "is my stuff actually safe right now?".
  const lastAny=(ACCOUNTS||[]).map(a=>a.lastSuccess).filter(Boolean).sort().pop()
    ||(s.connectors||[]).map(c=>c.last_run_at).filter(Boolean).sort().pop();
  const fresh=lastAny?`<span class="badge good">${ic('check',11,2.6)}Last backup ${esc(relTime(lastAny)||'recently')}</span>`:'';
  let body=`<header class="dhead"><h1>Your archive</h1>
    <div class="dsub">${enc}${fresh}<span>Stored locally at <code class="path">${esc(s.home)}</code></span></div></header>`;
  if(s.connectors.length){
    body+=`<div class="stats">`
      +statTile(s.total_records.toLocaleString(),'Items','layers')
      +statTile(String(s.connectors.length),'Platforms','grid')
      +statTile(s.blob_count.toLocaleString(),'Media files','image')
      +statTile(s.total_bytes_h,'On disk','drive')
      +`</div><div class="seclabel">Platforms</div><div class="cards">`;
    body+=s.connectors.map((c,i)=>{
      const col=c.last_status==='error'?'var(--bad)':(c.stale?'var(--warn)':'var(--good)');
      const st=c.last_status==='error'?'Last backup failed':(c.last_status===null?'Never run':(c.stale?'Stale':'Up to date'));
      const when=c.last_run_at?' · '+dayLabel(dayKey(c.last_run_at)):'';
      return `<button class="pcard" data-i="${i}">
        <div class="h">${tile(c.connector,'lg')}<span class="pname">${esc(c.connector)}</span><span class="dot" style="background:${col}"></span></div>
        <div class="n">${c.records.toLocaleString()}</div>
        <div class="s">${st}${when}</div></button>`;
    }).join('')+`</div>`;
    pane.innerHTML=`<div class="dash"><div class="dashin">${body}</div></div>`;
  }else{
    pane.innerHTML=`<div class="dash"><div class="dashin" style="height:100%;display:flex;flex-direction:column">${body}
      <div style="flex:1;display:grid;place-items:center"><div class="empty">
        <span class="eicon">${ic('box',20,1.8)}</span>
        <h3>Nothing backed up yet</h3>
        <p>Connect an account once. This app then requests your official export, downloads it, and files it away on a schedule — automatically, on this Mac.</p>
        <button class="tbtn primary" id="dash-connect" style="margin-top:14px;height:34px">${ic('plug',14,2)} Connect a platform</button>
      </div></div></div></div>`;
    const dc=document.getElementById('dash-connect'); if(dc) dc.addEventListener('click',showConnect);
  }
  pane.querySelectorAll('.pcard').forEach(el=>{
    el.addEventListener('click',()=>openConnector(s.connectors[+el.dataset.i].connector));
  });
}

/* ---------- platform view ---------- */
async function openConnector(id){
  const my=++REQ;
  active=id; activeThread=null; activeSpecial=null; onConnect=false; renderRail();
  document.getElementById('app').classList.remove('mode-dash');
  const list=document.getElementById('list'); list.hidden=false;
  list.innerHTML=`<div class="hd"><h2>${tile(id,'lg')}<span>${esc(id)}</span></h2>
      <div class="hsub" id="hsub"></div>
      <div class="searchwrap">${ic('search',14,2)}<input class="search" id="q" type="search" placeholder="Search conversations" autocomplete="off" aria-label="Search conversations"></div></div>
    <div class="rows" id="rows">${skRows(8)}</div>`;
  document.getElementById('pane').innerHTML=`<div class="msgs"><div class="msgstream">${skMsgs()}</div></div>`;
  let data;
  try{ data=await (await fetch('/api/threads?connector='+encodeURIComponent(id))).json(); }
  catch(e){
    if(my!==REQ) return;
    document.getElementById('pane').innerHTML=emptyState('box','Could not load this platform','The local server did not respond.');
    // The list must not be left shimmering as if it were still loading.
    const rows=document.getElementById('rows');
    if(rows) rows.innerHTML=emptyState('box','Could not load','Nothing to show for this platform right now.');
    const hs=document.getElementById('hsub'); if(hs) hs.textContent='';
    return;
  }
  if(my!==REQ) return;
  SELF=data.self; THREADS=data.threads||[];
  SPECIALS=[];
  const tc=data.type_counts||{};
  for(const [ty,label,icn] of [['follower','Followers','users'],['following','Following','users'],['post','Posts','doc'],['saved','Saved','bookmark']]){
    if(tc[ty]) SPECIALS.push({ty,label,icn,count:tc[ty]});
  }
  // Photos and video live across records rather than as their own type, so
  // "Media" is a view over everything with attachments.
  SPECIALS.push({ty:'__media',label:'Photos & video',icn:'image',count:null});
  const parts=[];
  if(THREADS.length) parts.push(THREADS.length.toLocaleString()+(THREADS.length===1?' conversation':' conversations'));
  // Media has no count of its own (it spans every record type), so it simply
  // doesn't appear in the header tally.
  for(const sp of SPECIALS) if(sp.count!=null) parts.push(sp.count.toLocaleString()+' '+sp.label.toLowerCase());
  const hs=document.getElementById('hsub');
  hs.textContent=parts.join(' · '); hs.title=parts.join(' · ');
  document.getElementById('q').addEventListener('input',filterThreads);
  renderThreadRows(THREADS);
  if(THREADS.length) openThread(THREADS[0].thread);
  else if(SPECIALS.length) openSpecial(SPECIALS[0]);
  else document.getElementById('pane').innerHTML=emptyState('chat','No conversations','This archive has no message threads for '+esc(id)+' yet.');
}
function curQuery(){const el=document.getElementById('q');return (el&&el.value||'').trim();}
function matchFilter(t){const q=curQuery().toLowerCase();
  return (t.thread||'').toLowerCase().includes(q)||(t.last_text||'').toLowerCase().includes(q);}
function filterThreads(){ renderThreadRows(THREADS.filter(matchFilter)); }
function renderThreadRows(threads){
  VIS=threads;
  const rows=document.getElementById('rows'); if(!rows) return;
  let h='';
  if(SPECIALS.length){
    h+=`<div class="rows-sec">Collections</div>`;
    SPECIALS.forEach((sp,i)=>{
      h+=`<button class="row ${activeSpecial===sp.ty?'active':''}" data-kind="special" data-i="${i}">
        <span class="av sq">${ic(sp.icn,15,1.8)}</span>
        <span class="mid"><span class="t"><span class="nm">${sp.label}</span></span>
        <span class="pv">${sp.count==null?'from your chats and posts':sp.count.toLocaleString()+' items'}</span></span></button>`;
    });
  }
  if(threads.length){
    if(SPECIALS.length) h+=`<div class="rows-sec">Conversations</div>`;
    threads.forEach((t,i)=>{
      const yours=t.last_author&&t.last_author===SELF;
      h+=`<button class="row ${activeThread===t.thread?'active':''}" data-kind="thread" data-i="${i}">
        <span class="av" style="background:${avColor(t.thread)}">${initial(t.thread)}</span>
        <span class="mid"><span class="t"><span class="nm" dir="auto">${esc(t.thread)}</span><span class="tm">${fmtShort(t.last_at)}</span></span>
        <span class="pv">${yours?'<b>You:</b> ':''}${t.last_text?esc(t.last_text.slice(0,80)):'<i style="opacity:.75">Photo</i>'}</span></span></button>`;
    });
  }else if(THREADS.length){
    h+=emptyState('search','No results','No conversations match "'+esc(curQuery())+'".');
  }else if(!SPECIALS.length){
    h+=emptyState('chat','No conversations','Nothing to show here yet.');
  }
  rows.innerHTML=h;
  rows.querySelectorAll('.row').forEach(el=>{
    el.addEventListener('click',()=>{
      if(el.dataset.kind==='special') openSpecial(SPECIALS[+el.dataset.i]);
      else openThread(VIS[+el.dataset.i].thread);
    });
  });
}

/* ---------- transcript ---------- */
async function openThread(thread){
  const my=++REQ, conn=active;
  activeThread=thread; activeSpecial=null; renderThreadRows(THREADS.filter(matchFilter));
  const pane=document.getElementById('pane');
  pane.innerHTML=`<div class="top"><span class="av lg" style="background:${avColor(thread)}">${initial(thread)}</span>
      <div><div class="nm">${esc(thread)}</div><div class="sub">&nbsp;</div></div></div>
    <div class="msgs"><div class="msgstream">${skMsgs()}</div></div>`;
  let msgs;
  try{ msgs=await (await fetch(`/api/thread?connector=${encodeURIComponent(active)}&thread=${encodeURIComponent(thread)}`)).json(); }
  catch(e){ if(my===REQ) pane.innerHTML=emptyState('box','Could not load this conversation','The local server did not respond.'); return; }
  if(my!==REQ||conn!==active) return;
  let day='', prev=null, body='';
  for(const m of msgs){
    const k=dayKey(m.created_at);
    if(k&&k!==day){ day=k; prev=null; body+=`<div class="daysep"><span>${dayLabel(k)}</span></div>`; }
    const me=!!(m.author&&m.author===SELF);
    const cont=prev!==null&&prev===m.author;
    const att=mediaList(m);
    body+=`<div class="msg${me?' me':''}${cont?' cont':''}">`
      +((me||cont)?'':`<span class="mav" style="background:${avColor(m.author)}">${initial(m.author)}</span>`)
      +`<div class="bubble">${(me||cont||!m.author)?'':`<div class="who" style="color:${avColor(m.author)}" dir="auto">${esc(m.author)}</div>`}`
      +(att.length?attachments(att):'')
      +(m.text?`<div class="tx" dir="auto">${esc(m.text)}</div>`:(att.length?'':`<div class="tx" style="color:var(--text-3)">(no content)</div>`))
      +`<div class="tm">${fmtTime(m.created_at)}</div></div></div>`;
    prev=m.author;
  }
  pane.innerHTML=`
    <div class="top"><span class="av lg" style="background:${avColor(thread)}">${initial(thread)}</span>
      <div><div class="nm">${esc(thread)}</div>
      <div class="sub">${msgs.length.toLocaleString()} ${msgs.length===1?'message':'messages'} · archived from ${esc(active)}</div></div></div>
    <div class="msgs" id="msgs" tabindex="0" role="region" aria-label="Conversation">
      <div class="msgstream">${body||emptyState('chat','No messages','This conversation has no text messages in the archive.')}</div></div>`;
  const box=document.getElementById('msgs'); if(box) box.scrollTop=box.scrollHeight;
}

/* ---------- collections (followers / following / posts / saved) ---------- */
async function openSpecial(sp){
  const my=++REQ, conn=active;
  activeThread=null; activeSpecial=sp.ty; renderThreadRows(THREADS.filter(matchFilter));
  const pane=document.getElementById('pane');
  const isMedia=sp.ty==='__media';
  pane.innerHTML=`<div class="dash"><div class="dashin">
    <header class="dhead"><h1>${sp.label}</h1><div class="dsub"><span>&nbsp;</span></div></header>
    <div class="${isMedia?'mgrid':'fgrid'}">${'<div class="sk" style="height:'+(isMedia?'150px':'56px')+';border-radius:11px"></div>'.repeat(9)}</div></div></div>`;
  let rows;
  const url=isMedia
    ? `/api/records?connector=${encodeURIComponent(active)}&limit=4000`
    : `/api/records?connector=${encodeURIComponent(active)}&type=${encodeURIComponent(sp.ty)}&limit=1000`;
  try{ rows=await (await fetch(url)).json(); }
  catch(e){ if(my===REQ) pane.innerHTML=emptyState('box','Could not load '+sp.label.toLowerCase(),'The local server did not respond.'); return; }
  if(my!==REQ||conn!==active) return;

  if(isMedia){
    // One flat, chronological wall of everything with an attachment.
    const seen=new Set(), all=[];
    for(const r of rows) for(const s of mediaList(r)) if(!seen.has(s)){ seen.add(s); all.push(s); }
    const cells=all.map(s=>`<div class="cell">${mediaCell(s).replace(/^<span class="cell">|<\/span>$/g,'')}</div>`).join('');
    pane.innerHTML=`<div class="dash"><div class="dashin">
      <header class="dhead"><h1>Photos &amp; video</h1>
      <div class="dsub"><span>${all.length.toLocaleString()} file${all.length===1?'':'s'} archived from ${esc(active)} · click any to open</span></div></header>
      ${all.length?`<div class="mgrid" data-group="${escA(all.join(','))}">${cells}</div>`
        :emptyState('image','No photos yet','No photos or video were attached in this export.')}</div></div>`;
    return;
  }

  const grid=sp.ty==='follower'||sp.ty==='following';
  const items=rows.map(r=>{
    const name=r.author||r.text||'';
    const when=r.created_at?dayLabel(dayKey(r.created_at)):'';
    if(grid) return `<div class="fcard"><span class="av" style="background:${avColor(name)}">${initial(name)}</span>
      <span class="fmid"><span class="fnm">${esc(name)}</span><span class="fdt">${when}</span></span></div>`;
    const att=mediaList(r);
    return `<div class="listcard"><div class="lhd"><span class="who">${esc(name)}</span><span class="fdt">${when}</span></div>
      ${att.length?`<div style="margin-top:8px;max-width:420px">${attachments(att)}</div>`:''}
      ${r.text&&r.text!==name?`<div class="ltx">${esc(r.text)}</div>`:''}</div>`;
  }).join('');
  pane.innerHTML=`<div class="dash"><div class="dashin">
    <header class="dhead"><h1>${sp.label}</h1>
    <div class="dsub"><span>${rows.length.toLocaleString()} ${sp.label.toLowerCase()} archived from ${esc(active)}</span></div></header>
    ${items?`<div class="${grid?'fgrid':'llist'}">${items}</div>`:emptyState('users','Nothing here','This collection is empty in the archive.')}</div></div>`;
}
boot();
</script>
</body>
</html>
"""
