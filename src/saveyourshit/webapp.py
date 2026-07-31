"""A local web app: dashboard + data viewer, served on 127.0.0.1 only.

This is the visual face of Save Your Shit without shipping an Electron bundle. A
tiny stdlib HTTP server (no framework, no telemetry, loopback-only) serves a
single-page dashboard that reads the local archive and exposes two JSON
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
    query = (qs.get("q") or [""])[0]
    limit = int((qs.get("limit") or ["300"])[0])
    if query:
        return archive.index.search(query, connector=connector or None, limit=limit)
    rows = archive.index.iter_records(limit=limit * 4)
    if connector:
        rows = [r for r in rows if r["connector"] == connector]
    return rows[:limit]


def handle(
    path: str, qs: dict[str, list[str]], archive: Archive, config: Config
) -> tuple[int, str, bytes]:
    """Pure router: returns (status_code, content_type, body)."""
    if path == "/" or path == "/index.html":
        return 200, "text/html; charset=utf-8", INDEX_HTML.encode("utf-8")
    if path == "/api/status":
        return 200, "application/json", json.dumps(api_status(archive, config)).encode()
    if path == "/api/records":
        return 200, "application/json", json.dumps(api_records(archive, qs)).encode()
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
# The single-page UI
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Save Your Shit</title>
<style>
  :root{ --bg:#0b0c10; --panel:#12141c; --panel2:#151824; --line:#20232e;
         --text:#e8eaf0; --dim:#9aa0ad; --accent:#6ea8fe; --good:#4ade80;
         --warn:#fbbf24; --bad:#f87171; }
  *{ box-sizing:border-box; }
  body{ margin:0; background:var(--bg); color:var(--text);
        font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  a{ color:var(--accent); text-decoration:none; }
  .app{ display:grid; grid-template-columns:240px 1fr; min-height:100vh; }
  aside{ background:var(--panel); border-right:1px solid var(--line); padding:20px 14px;
         position:sticky; top:0; height:100vh; overflow:auto; }
  .brand{ font-size:18px; font-weight:700; padding:4px 8px 16px; display:flex; gap:8px; align-items:center; }
  .nav a{ display:flex; justify-content:space-between; align-items:center; padding:9px 10px;
          border-radius:9px; color:var(--text); margin-bottom:2px; cursor:pointer; }
  .nav a:hover{ background:var(--panel2); }
  .nav a.active{ background:#1b2233; color:#fff; }
  .nav .pill{ font-size:11px; color:var(--dim); background:#0e1017; padding:1px 7px; border-radius:20px; }
  .dot{ width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:8px; }
  main{ padding:26px 32px 80px; max-width:1000px; }
  h1{ font-size:22px; margin:0 0 2px; }
  .sub{ color:var(--dim); margin-bottom:22px; }
  .stats{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:26px; }
  .stat{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:14px 16px; }
  .stat .n{ font-size:24px; font-weight:700; }
  .stat .l{ color:var(--dim); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  .cards{ display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:12px; }
  .card{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:14px 16px; cursor:pointer; }
  .card:hover{ border-color:#2c3346; }
  .card .top{ display:flex; justify-content:space-between; align-items:center; }
  .card .name{ font-weight:600; }
  .card .cnt{ font-size:22px; font-weight:700; margin:8px 0 2px; }
  .card .state{ font-size:12px; color:var(--dim); }
  .searchbar{ display:flex; gap:10px; margin:6px 0 18px; }
  input,select{ background:var(--panel2); border:1px solid var(--line); color:var(--text);
                border-radius:10px; padding:10px 12px; font-size:14px; }
  input{ flex:1; }
  .rec{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:11px 14px; margin-bottom:9px; }
  .rec .meta{ display:flex; gap:8px; align-items:center; font-size:12px; color:var(--dim); margin-bottom:3px; }
  .badge{ background:#1b2233; color:#c7d0e0; border-radius:6px; padding:1px 7px; font-size:11px; }
  .who{ font-weight:600; color:#e8eaf0; }
  .empty{ color:var(--dim); text-align:center; padding:60px; }
  .backlink{ color:var(--dim); cursor:pointer; font-size:13px; margin-bottom:14px; display:inline-block; }
</style>
</head>
<body>
<div class="app">
  <aside>
    <div class="brand">🛟 Save Your Shit</div>
    <div class="nav" id="nav"></div>
  </aside>
  <main id="main"><div class="empty">Loading your archive…</div></main>
</div>
<script>
const PLATFORM = {
  instagram:"📸", facebook:"👤", "facebook & messenger":"👤", discord:"🎮",
  twitter:"🐦", telegram:"✈️", reddit:"👽", whatsapp:"💬", google:"🔴",
  slack:"💼", snapchat:"👻", linkedin:"💼"
};
function icon(id){ return PLATFORM[id] || "🗂"; }
function esc(s){ const d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }
let STATUS=null, active=null;

async function load(){
  STATUS = await (await fetch('/api/status')).json();
  renderNav(); showDashboard();
}
function renderNav(){
  const nav = document.getElementById('nav');
  let html = `<a class="${active?'':'active'}" onclick="showDashboard()">`+
             `<span>🏠 Dashboard</span></a>`;
  for(const c of STATUS.connectors){
    const col = c.stale ? 'var(--warn)' : (c.last_status==='error'?'var(--bad)':'var(--good)');
    html += `<a class="${active===c.connector?'active':''}" onclick="showConnector('${c.connector}')">`+
      `<span><span class="dot" style="background:${col}"></span>${icon(c.connector)} ${esc(c.connector)}</span>`+
      `<span class="pill">${c.records.toLocaleString()}</span></a>`;
  }
  nav.innerHTML = html;
}
function showDashboard(){
  active=null; renderNav();
  const s=STATUS;
  let cards = s.connectors.map(c=>{
    const state = c.last_status===null ? 'never run'
      : c.stale ? '⚠ stale' : (c.last_status==='error'?'error':'✓ up to date');
    return `<div class="card" onclick="showConnector('${c.connector}')">
      <div class="top"><span class="name">${icon(c.connector)} ${esc(c.connector)}</span></div>
      <div class="cnt">${c.records.toLocaleString()}</div>
      <div class="state">${state} · last ${(c.last_run_at||'—').slice(0,10)}</div></div>`;
  }).join('');
  document.getElementById('main').innerHTML = `
    <h1>Your archive</h1>
    <div class="sub">${s.encrypted?'🔒 encrypted':'⚠ not encrypted'} · everything below lives on your machine at
      <code>${esc(s.home)}</code></div>
    <div class="stats">
      <div class="stat"><div class="n">${s.total_records.toLocaleString()}</div><div class="l">items</div></div>
      <div class="stat"><div class="n">${s.connectors.length}</div><div class="l">platforms</div></div>
      <div class="stat"><div class="n">${s.blob_count.toLocaleString()}</div><div class="l">media files</div></div>
      <div class="stat"><div class="n">${s.total_bytes_h}</div><div class="l">on disk</div></div>
    </div>
    <div class="cards">${cards || '<div class="empty">No data yet.</div>'}</div>`;
}
async function showConnector(id){
  active=id; renderNav();
  document.getElementById('main').innerHTML = `
    <span class="backlink" onclick="showDashboard()">← Dashboard</span>
    <h1>${icon(id)} ${esc(id)}</h1>
    <div class="searchbar">
      <input id="q" placeholder="Search ${esc(id)}…" oninput="refresh('${id}')" autofocus>
    </div>
    <div id="list"><div class="empty">Loading…</div></div>`;
  refresh(id);
}
async function refresh(id){
  const q = (document.getElementById('q')||{}).value || '';
  const rows = await (await fetch(`/api/records?connector=${encodeURIComponent(id)}&q=${encodeURIComponent(q)}&limit=300`)).json();
  const list = document.getElementById('list');
  if(!rows.length){ list.innerHTML='<div class="empty">No matches.</div>'; return; }
  list.innerHTML = rows.map(r=>`
    <div class="rec">
      <div class="meta"><span class="badge">${esc(r.type)}</span>
        <span>${esc((r.created_at||'').slice(0,19).replace('T',' '))}</span>
        ${r.thread?`<span>· ${esc(r.thread)}</span>`:''}</div>
      <div><span class="who">${esc(r.author||'')}</span> ${esc(r.text||'')}</div>
    </div>`).join('');
}
load();
</script>
</body>
</html>
"""
