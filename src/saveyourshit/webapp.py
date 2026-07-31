"""A local web app: a chat-first archive browser, served on 127.0.0.1 only.

This is the visual face of Save Your Shit without shipping an Electron bundle. A
tiny stdlib HTTP server (no framework, no telemetry, loopback-only) serves a
single-page messaging UI that reads the local archive and exposes a few JSON
endpoints. Nothing leaves the machine.

Routing is a pure function (:func:`handle`) so it is trivially testable without
binding a socket.
"""

from __future__ import annotations

import json
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
    limit = int((qs.get("limit") or ["300"])[0])
    if query:
        return archive.index.search(query, connector=connector or None, limit=limit)
    rows = archive.index.iter_records(limit=limit * 8)
    if connector:
        rows = [r for r in rows if r["connector"] == connector]
    if type_:
        rows = [r for r in rows if r["type"] == type_]
    return rows[:limit]


def api_threads(archive: Archive, connector: str) -> dict:
    """Conversation list + type breakdown for the chat UI."""
    return {
        "self": archive.index.self_author(connector),
        "threads": archive.index.threads(connector),
        "type_counts": archive.index.counts_by_type(connector),
    }


def api_thread(archive: Archive, connector: str, thread: str) -> list[dict]:
    return archive.index.thread_messages(connector, thread)


def handle(
    path: str, qs: dict[str, list[str]], archive: Archive, config: Config
) -> tuple[int, str, bytes]:
    """Pure router: returns (status_code, content_type, body)."""
    if path in ("/", "/index.html"):
        return 200, "text/html; charset=utf-8", INDEX_HTML.encode("utf-8")
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
            self.end_headers()
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
<title>Save Your Shit</title>
<style>
  /* ------------------------------------------------------------------ */
  /* Design tokens — neutral dark theme with a cobalt-blue brand         */
  /* ------------------------------------------------------------------ */
  :root{
    --bg:#100f0d;            /* app background            */
    --rail:#151311;          /* left rail surface         */
    --panel:#191815;         /* cards, list column        */
    --panel-2:#201e1a;       /* raised surface            */
    --hover:#242220;         /* hover wash                */
    --active:#2b2824;        /* selected wash             */
    --line:#262421;          /* hairline borders          */
    --line-2:#36322b;        /* stronger borders          */
    --text:#ece7df;          /* primary text              */
    --text-2:#a69f92;        /* secondary text            */
    --text-3:#7b7468;        /* tertiary / meta text      */
    --brand:#2563eb;         /* primary brand blue        */
    --brand-strong:#1d4ed8;  /* pressed / lower gradient  */
    --accent:var(--brand);
    --accent-soft:rgba(37,99,235,.18);
    --good:#82c98f; --warn:#d9b355; --bad:#d97862;
    --bubble-them:#232019;
    --bubble-them-line:#322e26;
    --bubble-me:linear-gradient(180deg,#3b82f6,var(--brand-strong));
    --bubble-me-text:#fff;
    --shadow-card:0 8px 22px rgba(0,0,0,.35);
    --r-sm:8px; --r-md:11px; --r-lg:14px;
  }
  *{ box-sizing:border-box; }
  html,body{ height:100%; }
  body{ margin:0; background:var(--bg); color:var(--text);
        font:13.5px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
        -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility; }
  button{ font:inherit; color:inherit; background:none; border:0; padding:0; text-align:left; cursor:pointer; }
  code{ font:11.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  [hidden]{ display:none !important; }
  ::-webkit-scrollbar{ width:10px; height:10px; }
  ::-webkit-scrollbar-thumb{ background:#2e2b26; border-radius:8px; border:3px solid transparent; background-clip:content-box; }
  ::-webkit-scrollbar-thumb:hover{ background:#3a362f; border:3px solid transparent; background-clip:content-box; }
  ::-webkit-scrollbar-track{ background:transparent; }
  ::selection{ background:rgba(217,160,91,.30); }
  :focus-visible{ outline:2px solid var(--accent); outline-offset:-2px; border-radius:var(--r-sm); }

  .app{ display:grid; grid-template-columns:246px minmax(280px,330px) 1fr; height:100vh; }
  .app.mode-dash{ grid-template-columns:246px 1fr; }

  /* ------------------------------------------------------------------ */
  /* Left rail                                                           */
  /* ------------------------------------------------------------------ */
  .rail{ background:var(--rail); border-right:1px solid var(--line); padding:16px 12px; overflow-y:auto; overflow-x:hidden; }
  .brand{ display:flex; align-items:center; gap:10px; font-weight:650; font-size:14px;
          letter-spacing:-.01em; padding:2px 8px 18px; white-space:nowrap; }
  .brand .mark{ width:28px; height:28px; border-radius:9px; flex:none; color:#fff;
                background:linear-gradient(160deg,#3b82f6,var(--brand-strong));
                box-shadow:inset 0 1px 0 rgba(255,255,255,.25);
                display:grid; place-items:center; }
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

  .tile{ width:22px; height:22px; border-radius:6px; display:grid; place-items:center;
         font-size:11px; font-weight:700; color:#fff;
         box-shadow:inset 0 0 0 1px rgba(255,255,255,.14), 0 1px 2px rgba(0,0,0,.4); }
  .tile.lg{ width:30px; height:30px; border-radius:9px; font-size:14px; }

  /* ------------------------------------------------------------------ */
  /* Middle column — conversation list                                   */
  /* ------------------------------------------------------------------ */
  .list{ background:var(--panel); border-right:1px solid var(--line); display:flex; flex-direction:column; min-width:0; min-height:0; overflow:hidden; }
  .list .hd{ padding:18px 14px 12px; border-bottom:1px solid var(--line); }
  .list .hd h2{ margin:0 0 3px; font-size:15px; font-weight:650; letter-spacing:-.01em;
                text-transform:capitalize; display:flex; align-items:center; gap:10px; }
  .list .hsub{ font-size:11.5px; color:var(--text-3); padding-left:40px; margin-bottom:12px;
               font-variant-numeric:tabular-nums; min-height:14px; }
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
  .pane .top .nm{ font-weight:650; font-size:14.5px; letter-spacing:-.01em; }
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
  .badge.good{ color:var(--good); border-color:rgba(130,201,143,.32); background:rgba(130,201,143,.08); }
  .badge.warn{ color:var(--warn); border-color:rgba(217,179,85,.32); background:rgba(217,179,85,.08); }
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
    .brand{ justify-content:center; padding:2px 0 16px; }
    .brand .bt{ display:none; }
    .nav-item{ justify-content:center; padding:9px 0; }
    .nav-item .nm,.nav-item .ct,.nav-item .dot{ display:none; }
    .sec{ display:none; }
    .dash{ padding:26px 24px; }
    .msgs{ padding:10px 16px 24px; }
    .msg{ max-width:86%; }
  }
</style>
</head>
<body>
<div class="app mode-dash" id="app">
  <nav class="rail" id="rail">
    <div class="brand" aria-label="Save Your Shit"><span class="mark"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/></svg></span><span class="bt">Save Your Shit</span></div>
  </nav>
  <section class="list" id="list" hidden></section>
  <main class="pane" id="pane"><div class="boot"><div class="spinner" role="status" aria-label="Loading"></div></div></main>
</div>
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
  box:'<path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5"/><path d="M12 13v8"/>',
  chat:'<path d="M21 12a8 8 0 0 1-8 8H5l-2 2V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z"/>',
  users:'<path d="M17 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2"/><circle cx="10" cy="7" r="4"/><path d="M21 21v-2a4 4 0 0 0-3-3.9"/><path d="M15 3.1a4 4 0 0 1 0 7.8"/>',
  lock:'<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  unlock:'<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.7-1.4"/>',
  layers:'<path d="M12 2l9 5-9 5-9-5z"/><path d="M3 12l9 5 9-5"/><path d="M3 17l9 5 9-5"/>',
  image:'<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="M21 15l-5-5-11 11"/>',
  drive:'<rect x="2" y="7" width="20" height="10" rx="2"/><path d="M6.5 12h.01"/><path d="M10.5 12h7"/>',
  doc:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
  bookmark:'<path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>'
};
/* ---------- tiny helpers ---------- */
function ic(n,s,w){s=s||16;w=w||2;return `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${PATHS[n]||''}</svg>`;}
function brandMark(s){s=s||15;return `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/></svg>`;}
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
function escA(s){return esc(s).replace(/"/g,'&quot;');}
function brandColor(id){return BRAND[id]||"#4a463f";}
function tile(id,cls){return `<span class="tile ${cls||''}" style="background:${brandColor(id)}">${esc((id[0]||'?').toUpperCase())}</span>`;}
function avColor(s){let h=0;for(const c of (s||'?'))h=(h*31+c.charCodeAt(0))>>>0;return AV[h%AV.length];}
function initial(s){return esc((s||'?').trim()[0]?.toUpperCase()||'?');}
function fmtTime(s){return (s||'').slice(11,16);}
function dayKey(s){return (s||'').slice(0,10);}
function dayLabel(k){if(!k)return'';const p=k.split('-');const mi=+p[1]-1;if(!(mi>=0&&mi<12))return k;return MONTHS[mi]+' '+(+p[2])+', '+p[0];}
function fmtShort(s){const k=dayKey(s);if(!k)return'';const p=k.split('-');const mi=+p[1]-1;if(!(mi>=0&&mi<12))return k;
  return +p[0]===new Date().getFullYear()?MONTHS[mi]+' '+(+p[2]):MONTHS[mi]+' '+p[0];}
function fmtCompact(n){if(n>=1e6)return (n/1e6).toFixed(1).replace(/\.0$/,'')+'M';
  if(n>=1e4)return Math.round(n/1e3)+'k';if(n>=1e3)return (n/1e3).toFixed(1).replace(/\.0$/,'')+'k';
  return String(n);}
function emptyState(icon,title,html){return `<div class="empty"><span class="eicon">${ic(icon,20,1.8)}</span><h3>${title}</h3><p>${html}</p></div>`;}
function skRows(n){let h='';for(let i=0;i<n;i++)h+=`<div class="sk-row"><div class="sk sk-av"></div><div class="sk-mid"><div class="sk sk-l1" style="width:${45+((i*13)%30)}%"></div><div class="sk sk-l2" style="width:${65+((i*17)%25)}%"></div></div></div>`;return h;}
function skMsgs(){let h='';const w=[46,58,34,52,40,62];for(let i=0;i<6;i++)h+=`<div class="sk sk-bubble ${i%3===2?'r':''}" style="width:${w[i]}%"></div>`;return h;}

/* ---------- state ---------- */
let STATUS=null, active=null, activeThread=null, activeSpecial=null, SELF=null;
let THREADS=[], SPECIALS=[], VIS=[];

/* ---------- boot ---------- */
async function boot(){
  const pane=document.getElementById('pane');
  try{ STATUS=await (await fetch('/api/status')).json(); }
  catch(e){ pane.innerHTML=emptyState('box','Could not load the archive','The local server did not respond. Close this tab and run <code>syt open</code> again.'); return; }
  renderRail(); showDashboard();
}

/* ---------- left rail ---------- */
function renderRail(){
  const r=document.getElementById('rail');
  let h=`<div class="brand" aria-label="Save Your Shit"><span class="mark">${brandMark()}</span><span class="bt">Save Your Shit</span></div>`;
  h+=`<button class="nav-item ${active?'':'active'}" data-nav="dash" title="Overview"><span class="ico">${ic('grid',16,1.8)}</span><span class="nm">Overview</span></button>`;
  if(STATUS.connectors.length) h+=`<div class="sec">Platforms</div>`;
  STATUS.connectors.forEach((c,i)=>{
    const col=c.stale?'var(--warn)':(c.last_status==='error'?'var(--bad)':'var(--good)');
    h+=`<button class="nav-item ${active===c.connector?'active':''}" data-nav="conn" data-i="${i}" title="${escA(c.connector)}">`
      +`<span class="ico">${tile(c.connector)}</span><span class="nm">${esc(c.connector)}</span>`
      +`<span class="dot" style="background:${col}"></span><span class="ct">${fmtCompact(c.records)}</span></button>`;
  });
  r.innerHTML=h;
  r.querySelectorAll('[data-nav]').forEach(el=>{
    el.addEventListener('click',()=>{
      if(el.dataset.nav==='dash') showDashboard();
      else openConnector(STATUS.connectors[+el.dataset.i].connector);
    });
  });
}

/* ---------- dashboard ---------- */
function statTile(n,label,icon){
  return `<div class="stat"><span class="ic">${ic(icon,16,1.7)}</span><div class="n">${n}</div><div class="l">${label}</div></div>`;
}
function showDashboard(){
  active=null; activeThread=null; activeSpecial=null; renderRail();
  document.getElementById('list').hidden=true;
  document.getElementById('app').classList.add('mode-dash');
  const s=STATUS, pane=document.getElementById('pane');
  const enc=s.encrypted
    ?`<span class="badge good">${ic('lock',11,2.2)}Encrypted</span>`
    :`<span class="badge warn">${ic('unlock',11,2.2)}Not encrypted</span>`;
  let body=`<header class="dhead"><h1>Your archive</h1>
    <div class="dsub">${enc}<span>Stored locally at <code class="path">${esc(s.home)}</code></span></div></header>`;
  if(s.connectors.length){
    body+=`<div class="stats">`
      +statTile(s.total_records.toLocaleString(),'Items','layers')
      +statTile(String(s.connectors.length),'Platforms','grid')
      +statTile(s.blob_count.toLocaleString(),'Media files','image')
      +statTile(s.total_bytes_h,'On disk','drive')
      +`</div><div class="seclabel">Platforms</div><div class="cards">`;
    body+=s.connectors.map((c,i)=>{
      const col=c.stale?'var(--warn)':(c.last_status==='error'?'var(--bad)':'var(--good)');
      const st=c.last_status===null?'Never run':(c.stale?'Stale':(c.last_status==='error'?'Error':'Up to date'));
      const when=c.last_run_at?' · '+dayLabel(dayKey(c.last_run_at)):'';
      return `<button class="pcard" data-i="${i}">
        <div class="h">${tile(c.connector,'lg')}<span class="pname">${esc(c.connector)}</span><span class="dot" style="background:${col}"></span></div>
        <div class="n">${c.records.toLocaleString()}</div>
        <div class="s">${st}${when}</div></button>`;
    }).join('')+`</div>`;
    pane.innerHTML=`<div class="dash"><div class="dashin">${body}</div></div>`;
  }else{
    pane.innerHTML=`<div class="dash"><div class="dashin" style="height:100%;display:flex;flex-direction:column">${body}
      <div style="flex:1;display:grid;place-items:center">${emptyState('box','No data yet',
      'Download an export, then choose <strong>Archive → Add Export…</strong> or drag it into this window.')}</div></div></div>`;
  }
  pane.querySelectorAll('.pcard').forEach(el=>{
    el.addEventListener('click',()=>openConnector(s.connectors[+el.dataset.i].connector));
  });
}

/* ---------- platform view ---------- */
async function openConnector(id){
  active=id; activeThread=null; activeSpecial=null; renderRail();
  document.getElementById('app').classList.remove('mode-dash');
  const list=document.getElementById('list'); list.hidden=false;
  list.innerHTML=`<div class="hd"><h2>${tile(id,'lg')}<span>${esc(id)}</span></h2>
      <div class="hsub" id="hsub"></div>
      <div class="searchwrap">${ic('search',14,2)}<input class="search" id="q" type="search" placeholder="Search conversations" autocomplete="off" aria-label="Search conversations"></div></div>
    <div class="rows" id="rows">${skRows(8)}</div>`;
  document.getElementById('pane').innerHTML=`<div class="msgs"><div class="msgstream">${skMsgs()}</div></div>`;
  let data;
  try{ data=await (await fetch('/api/threads?connector='+encodeURIComponent(id))).json(); }
  catch(e){ if(active===id) document.getElementById('pane').innerHTML=emptyState('box','Could not load this platform','The local server did not respond.'); return; }
  if(active!==id) return;
  SELF=data.self; THREADS=data.threads||[];
  SPECIALS=[];
  const tc=data.type_counts||{};
  for(const [ty,label,icn] of [['follower','Followers','users'],['following','Following','users'],['post','Posts','doc'],['saved','Saved','bookmark']]){
    if(tc[ty]) SPECIALS.push({ty,label,icn,count:tc[ty]});
  }
  const parts=[];
  if(THREADS.length) parts.push(THREADS.length.toLocaleString()+(THREADS.length===1?' conversation':' conversations'));
  for(const sp of SPECIALS) parts.push(sp.count.toLocaleString()+' '+sp.label.toLowerCase());
  document.getElementById('hsub').textContent=parts.join(' · ');
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
        <span class="pv">${sp.count.toLocaleString()} items</span></span></button>`;
    });
  }
  if(threads.length){
    if(SPECIALS.length) h+=`<div class="rows-sec">Conversations</div>`;
    threads.forEach((t,i)=>{
      const yours=t.last_author&&t.last_author===SELF;
      h+=`<button class="row ${activeThread===t.thread?'active':''}" data-kind="thread" data-i="${i}">
        <span class="av" style="background:${avColor(t.thread)}">${initial(t.thread)}</span>
        <span class="mid"><span class="t"><span class="nm">${esc(t.thread)}</span><span class="tm">${fmtShort(t.last_at)}</span></span>
        <span class="pv">${yours?'<b>You:</b> ':''}${esc((t.last_text||'').slice(0,80))}</span></span></button>`;
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
  activeThread=thread; activeSpecial=null; renderThreadRows(THREADS.filter(matchFilter));
  const pane=document.getElementById('pane');
  pane.innerHTML=`<div class="top"><span class="av lg" style="background:${avColor(thread)}">${initial(thread)}</span>
      <div><div class="nm">${esc(thread)}</div><div class="sub">&nbsp;</div></div></div>
    <div class="msgs"><div class="msgstream">${skMsgs()}</div></div>`;
  let msgs;
  try{ msgs=await (await fetch(`/api/thread?connector=${encodeURIComponent(active)}&thread=${encodeURIComponent(thread)}`)).json(); }
  catch(e){ if(activeThread===thread) pane.innerHTML=emptyState('box','Could not load this conversation','The local server did not respond.'); return; }
  if(activeThread!==thread) return;
  let day='', prev=null, body='';
  for(const m of msgs){
    const k=dayKey(m.created_at);
    if(k&&k!==day){ day=k; prev=null; body+=`<div class="daysep"><span>${dayLabel(k)}</span></div>`; }
    const me=!!(m.author&&m.author===SELF);
    const cont=prev!==null&&prev===m.author;
    body+=`<div class="msg${me?' me':''}${cont?' cont':''}">`
      +((me||cont)?'':`<span class="mav" style="background:${avColor(m.author)}">${initial(m.author)}</span>`)
      +`<div class="bubble">${(me||cont)?'':`<div class="who" style="color:${avColor(m.author)}">${esc(m.author)}</div>`}`
      +`<div class="tx">${esc(m.text)}</div><div class="tm">${fmtTime(m.created_at)}</div></div></div>`;
    prev=m.author;
  }
  pane.innerHTML=`
    <div class="top"><span class="av lg" style="background:${avColor(thread)}">${initial(thread)}</span>
      <div><div class="nm">${esc(thread)}</div>
      <div class="sub">${msgs.length.toLocaleString()} ${msgs.length===1?'message':'messages'} · archived from ${esc(active)}</div></div></div>
    <div class="msgs" id="msgs"><div class="msgstream">${body||emptyState('chat','No messages','This conversation has no text messages in the archive.')}</div></div>`;
  const box=document.getElementById('msgs'); if(box) box.scrollTop=box.scrollHeight;
}

/* ---------- collections (followers / following / posts / saved) ---------- */
async function openSpecial(sp){
  activeThread=null; activeSpecial=sp.ty; renderThreadRows(THREADS.filter(matchFilter));
  const pane=document.getElementById('pane');
  pane.innerHTML=`<div class="dash"><div class="dashin">
    <header class="dhead"><h1>${sp.label}</h1><div class="dsub"><span>&nbsp;</span></div></header>
    <div class="fgrid">${'<div class="sk" style="height:56px;border-radius:11px"></div>'.repeat(9)}</div></div></div>`;
  let rows;
  try{ rows=await (await fetch(`/api/records?connector=${encodeURIComponent(active)}&type=${encodeURIComponent(sp.ty)}&limit=1000`)).json(); }
  catch(e){ if(activeSpecial===sp.ty) pane.innerHTML=emptyState('box','Could not load '+sp.label.toLowerCase(),'The local server did not respond.'); return; }
  if(activeSpecial!==sp.ty) return;
  const grid=sp.ty==='follower'||sp.ty==='following';
  const items=rows.map(r=>{
    const name=r.author||r.text||'';
    const when=r.created_at?dayLabel(dayKey(r.created_at)):'';
    if(grid) return `<div class="fcard"><span class="av" style="background:${avColor(name)}">${initial(name)}</span>
      <span class="fmid"><span class="fnm">${esc(name)}</span><span class="fdt">${when}</span></span></div>`;
    return `<div class="listcard"><div class="lhd"><span class="who">${esc(name)}</span><span class="fdt">${when}</span></div>
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
