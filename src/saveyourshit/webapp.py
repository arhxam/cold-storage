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
  :root{
    --bg:#0a0b0f; --rail:#0e0f15; --panel:#12141c; --panel2:#161923; --hover:#1b1f2b;
    --line:#20232e; --line2:#282c3a; --text:#eef0f6; --dim:#9297a6; --dim2:#6b7180;
    --accent:#3d7bfd; --accent2:#5b8cff; --good:#3ddc84; --warn:#ffb020; --bad:#ff5c5c;
    --bubble-me:linear-gradient(180deg,#3d7bfd,#2f6bf0); --bubble-them:#1b1f2b;
  }
  *{ box-sizing:border-box; }
  html,body{ height:100%; }
  body{ margin:0; background:var(--bg); color:var(--text);
        font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;
        -webkit-font-smoothing:antialiased; }
  ::-webkit-scrollbar{ width:9px; height:9px; }
  ::-webkit-scrollbar-thumb{ background:#242836; border-radius:6px; }
  ::-webkit-scrollbar-track{ background:transparent; }
  .app{ display:grid; grid-template-columns:236px minmax(280px,340px) 1fr; height:100vh; }

  /* ---- rail: platforms ---- */
  .rail{ background:var(--rail); border-right:1px solid var(--line); padding:16px 12px; overflow:auto; }
  .brand{ display:flex; align-items:center; gap:9px; font-weight:700; font-size:16px; padding:6px 8px 14px; }
  .rail a{ display:flex; align-items:center; gap:10px; padding:9px 10px; border-radius:10px;
           color:var(--text); cursor:pointer; margin-bottom:2px; font-size:13.5px; }
  .rail a:hover{ background:var(--hover); }
  .rail a.active{ background:#1d2740; color:#fff; box-shadow:inset 2px 0 0 var(--accent2); }
  .rail a .ico{ width:22px; text-align:center; font-size:15px; }
  .rail a .nm{ flex:1; text-transform:capitalize; }
  .rail a .ct{ font-size:11px; color:var(--dim2); background:#0a0c12; padding:1px 7px; border-radius:20px; }
  .rail .sec{ color:var(--dim2); font-size:10.5px; text-transform:uppercase; letter-spacing:.08em;
              padding:14px 10px 6px; }
  .dot{ width:7px; height:7px; border-radius:50%; display:inline-block; }

  /* ---- middle: conversations ---- */
  .list{ background:var(--panel); border-right:1px solid var(--line); display:flex; flex-direction:column; min-width:0; }
  .list .hd{ padding:16px 16px 10px; border-bottom:1px solid var(--line); }
  .list .hd h2{ margin:0 0 10px; font-size:16px; text-transform:capitalize; }
  .search{ width:100%; background:var(--panel2); border:1px solid var(--line2); color:var(--text);
           border-radius:9px; padding:8px 11px; font-size:13px; outline:none; }
  .search:focus{ border-color:var(--accent); }
  .rows{ overflow:auto; flex:1; padding:6px; }
  .row{ display:flex; gap:11px; padding:10px 11px; border-radius:11px; cursor:pointer; align-items:center; }
  .row:hover{ background:var(--hover); }
  .row.active{ background:#1d2740; }
  .av{ width:38px; height:38px; border-radius:50%; flex:none; display:grid; place-items:center;
       font-weight:600; font-size:14px; color:#fff; }
  .row .mid{ flex:1; min-width:0; }
  .row .t{ display:flex; justify-content:space-between; gap:8px; }
  .row .nm{ font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .row .tm{ font-size:11px; color:var(--dim2); white-space:nowrap; }
  .row .pv{ color:var(--dim); font-size:12.5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .chip{ font-size:11px; color:var(--dim); }

  /* ---- right: transcript ---- */
  .pane{ display:flex; flex-direction:column; min-width:0; background:
         radial-gradient(1200px 500px at 70% -10%, #101827 0%, transparent 60%), var(--bg); }
  .pane .top{ padding:15px 22px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:12px; }
  .pane .top .nm{ font-weight:650; font-size:15px; }
  .pane .top .sub{ color:var(--dim2); font-size:12px; }
  .msgs{ flex:1; overflow:auto; padding:22px 22px 30px; display:flex; flex-direction:column; gap:3px; }
  .daysep{ align-self:center; color:var(--dim2); font-size:11px; background:var(--panel);
           border:1px solid var(--line); padding:3px 12px; border-radius:20px; margin:16px 0 10px; }
  .msg{ display:flex; gap:9px; max-width:74%; margin:1px 0; }
  .msg.me{ align-self:flex-end; flex-direction:row-reverse; }
  .msg .bubble{ padding:8px 13px; border-radius:16px; background:var(--bubble-them);
                border:1px solid var(--line2); }
  .msg.me .bubble{ background:var(--bubble-me); border:none; color:#fff; }
  .msg .who{ font-size:11px; color:var(--accent2); font-weight:600; margin-bottom:2px; }
  .msg.me .who{ display:none; }
  .msg .tx{ white-space:pre-wrap; word-break:break-word; }
  .msg .tm{ font-size:10px; color:var(--dim2); margin-top:3px; text-align:right; }
  .msg.me .tm{ color:#cdd8ff; }
  .msg .mav{ width:26px; height:26px; border-radius:50%; flex:none; align-self:flex-end;
             display:grid; place-items:center; font-size:11px; color:#fff; font-weight:600; }
  .msg.me .mav{ display:none; }

  /* ---- dashboard ---- */
  .dash{ padding:30px 34px; overflow:auto; }
  .dash h1{ font-size:23px; margin:0 0 4px; }
  .dash .sub{ color:var(--dim); margin-bottom:24px; font-size:13px; }
  .stats{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:26px; }
  .stat{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:15px 17px; }
  .stat .n{ font-size:25px; font-weight:700; }
  .stat .l{ color:var(--dim2); font-size:11px; text-transform:uppercase; letter-spacing:.05em; margin-top:2px; }
  .cards{ display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); gap:12px; }
  .pcard{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:15px 17px; cursor:pointer; }
  .pcard:hover{ border-color:var(--line2); background:var(--panel2); }
  .pcard .h{ display:flex; align-items:center; gap:9px; font-weight:600; text-transform:capitalize; }
  .pcard .n{ font-size:23px; font-weight:700; margin:9px 0 2px; }
  .pcard .s{ font-size:12px; color:var(--dim2); }
  .empty{ color:var(--dim2); display:grid; place-items:center; height:100%; text-align:center; padding:40px; }
  .listcard{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:11px 15px; margin-bottom:8px; }
  .listcard .who{ font-weight:600; }
  .listcard .meta{ font-size:12px; color:var(--dim2); }
</style>
</head>
<body>
<div class="app">
  <nav class="rail" id="rail"></nav>
  <section class="list" id="list" style="display:none"></section>
  <section class="pane" id="pane"><div class="empty">Loading your archive…</div></section>
</div>
<script>
const EMOJI={instagram:"📸",facebook:"👤",discord:"🎮",twitter:"🐦",telegram:"✈️",
  reddit:"👽",whatsapp:"💬",google:"🔴",slack:"💼",snapchat:"👻",linkedin:"🔗"};
const AV=["#e0567a","#f0883e","#3ddc84","#3d7bfd","#a06bff","#00b8c4","#ff6b6b","#f2c14e"];
function emo(id){return EMOJI[id]||"🗂";}
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
function avColor(s){let h=0;for(const c of (s||'?'))h=(h*31+c.charCodeAt(0))>>>0;return AV[h%AV.length];}
function initial(s){return (s||'?').trim()[0]?.toUpperCase()||'?';}
function fmtTime(s){return (s||'').slice(11,16);}
function fmtDay(s){const d=(s||'').slice(0,10);return d;}
function fmtRel(s){return (s||'').slice(0,10);}
let STATUS=null, active=null, activeThread=null, SELF=null;

async function boot(){
  STATUS=await (await fetch('/api/status')).json();
  renderRail(); showDashboard();
}
function renderRail(){
  let h=`<div class="brand">🛟 Save Your Shit</div>`;
  h+=`<a class="${active?'':'active'}" onclick="showDashboard()"><span class="ico">🏠</span><span class="nm">Dashboard</span></a>`;
  h+=`<div class="sec">Platforms</div>`;
  for(const c of STATUS.connectors){
    const col=c.stale?'var(--warn)':(c.last_status==='error'?'var(--bad)':'var(--good)');
    h+=`<a class="${active===c.connector?'active':''}" onclick="openConnector('${c.connector}')">
      <span class="ico">${emo(c.connector)}</span><span class="nm">${esc(c.connector)}</span>
      <span class="dot" style="background:${col}"></span><span class="ct">${c.records.toLocaleString()}</span></a>`;
  }
  document.getElementById('rail').innerHTML=h;
}
function showDashboard(){
  active=null; activeThread=null; renderRail();
  document.getElementById('list').style.display='none';
  document.querySelector('.app').style.gridTemplateColumns='236px 1fr';
  const s=STATUS;
  const cards=s.connectors.map(c=>{
    const st=c.last_status===null?'never run':(c.stale?'⚠ stale':(c.last_status==='error'?'error':'✓ up to date'));
    return `<div class="pcard" onclick="openConnector('${c.connector}')">
      <div class="h">${emo(c.connector)} ${esc(c.connector)}</div>
      <div class="n">${c.records.toLocaleString()}</div>
      <div class="s">${st} · ${fmtRel(c.last_run_at)||'—'}</div></div>`;}).join('');
  document.getElementById('pane').innerHTML=`<div class="dash">
    <h1>Your archive</h1>
    <div class="sub">${s.encrypted?'🔒 encrypted':'⚠ not encrypted'} · lives on your machine at <code>${esc(s.home)}</code></div>
    <div class="stats">
      <div class="stat"><div class="n">${s.total_records.toLocaleString()}</div><div class="l">items</div></div>
      <div class="stat"><div class="n">${s.connectors.length}</div><div class="l">platforms</div></div>
      <div class="stat"><div class="n">${s.blob_count.toLocaleString()}</div><div class="l">media files</div></div>
      <div class="stat"><div class="n">${s.total_bytes_h}</div><div class="l">on disk</div></div>
    </div>
    <div class="cards">${cards||'<div class="empty">Nothing yet — run <code>syt ingest</code>.</div>'}</div></div>`;
}
let THREADS=[];
async function openConnector(id){
  active=id; activeThread=null; renderRail();
  const data=await (await fetch('/api/threads?connector='+encodeURIComponent(id))).json();
  SELF=data.self; THREADS=data.threads;
  document.querySelector('.app').style.gridTemplateColumns='236px minmax(280px,340px) 1fr';
  const list=document.getElementById('list'); list.style.display='flex';
  // special (non-message) sections
  const tc=data.type_counts||{};
  const specials=[];
  for(const [ty,label] of [['follower','Followers'],['following','Following'],['post','Posts'],['saved','Saved']]){
    if(tc[ty]) specials.push({ty,label,count:tc[ty]});
  }
  list.innerHTML=`<div class="hd"><h2>${emo(id)} ${esc(id)}</h2>
      <input class="search" id="q" placeholder="Search ${esc(id)}…" oninput="filterThreads()"></div>
    <div class="rows" id="rows"></div>`;
  window._specials=specials;
  renderThreadRows(THREADS);
  if(THREADS.length) openThread(THREADS[0].thread);
  else if(specials.length) openSpecial(specials[0].ty,specials[0].label);
  else document.getElementById('pane').innerHTML='<div class="empty">No conversations in this archive.</div>';
}
function renderThreadRows(threads){
  const rows=document.getElementById('rows'); window._vis=threads; let h='';
  (window._specials||[]).forEach((sp,i)=>{
    h+=`<div class="row" data-kind="special" data-i="${i}">
      <div class="av" style="background:#222736">${sp.label[0]}</div>
      <div class="mid"><div class="t"><span class="nm">${sp.label}</span></div>
      <div class="pv">${sp.count.toLocaleString()} items</div></div></div>`;
  });
  threads.forEach((t,i)=>{
    h+=`<div class="row ${activeThread===t.thread?'active':''}" data-kind="thread" data-i="${i}">
      <div class="av" style="background:${avColor(t.thread)}">${initial(t.thread)}</div>
      <div class="mid"><div class="t"><span class="nm">${esc(t.thread)}</span><span class="tm">${fmtRel(t.last_at)}</span></div>
      <div class="pv">${t.last_author&&t.last_author===SELF?'You: ':''}${esc((t.last_text||'').slice(0,60))}</div></div></div>`;
  });
  rows.innerHTML=h||'<div class="empty">No conversations.</div>';
  rows.querySelectorAll('.row').forEach(el=>{
    el.onclick=()=>{
      if(el.dataset.kind==='special'){ const sp=window._specials[+el.dataset.i]; openSpecial(sp.ty,sp.label); }
      else openThread(window._vis[+el.dataset.i].thread);
    };
  });
}
function filterThreads(){
  const q=(document.getElementById('q').value||'').toLowerCase();
  renderThreadRows(THREADS.filter(t=>(t.thread||'').toLowerCase().includes(q)||(t.last_text||'').toLowerCase().includes(q)));
}
async function openThread(thread){
  activeThread=thread; renderThreadRows(THREADS.filter(matchFilter));
  const msgs=await (await fetch(`/api/thread?connector=${encodeURIComponent(active)}&thread=${encodeURIComponent(thread)}`)).json();
  let day='', body='';
  for(const m of msgs){
    const d=fmtDay(m.created_at);
    if(d&&d!==day){ day=d; body+=`<div class="daysep">${d}</div>`; }
    const me=m.author&&m.author===SELF;
    body+=`<div class="msg ${me?'me':''}">
      ${me?'':`<div class="mav" style="background:${avColor(m.author)}">${initial(m.author)}</div>`}
      <div class="bubble">${me?'':`<div class="who">${esc(m.author)}</div>`}
      <div class="tx">${esc(m.text)}</div><div class="tm">${fmtTime(m.created_at)}</div></div></div>`;
  }
  document.getElementById('pane').innerHTML=`
    <div class="top"><div class="av" style="width:34px;height:34px;background:${avColor(thread)}">${initial(thread)}</div>
      <div><div class="nm">${esc(thread)}</div><div class="sub">${msgs.length} messages</div></div></div>
    <div class="msgs" id="msgs">${body||'<div class="empty">No messages.</div>'}</div>`;
  const box=document.getElementById('msgs'); if(box) box.scrollTop=box.scrollHeight;
}
function matchFilter(t){ const el=document.getElementById('q'); const q=(el&&el.value||'').toLowerCase();
  return (t.thread||'').toLowerCase().includes(q)||(t.last_text||'').toLowerCase().includes(q); }
async function openSpecial(ty,label){
  activeThread=null;
  const rows=await (await fetch(`/api/records?connector=${encodeURIComponent(active)}&type=${ty}&limit=1000`)).json();
  const body=rows.map(r=>`<div class="listcard"><span class="who">${esc(r.author||r.text||'')}</span>
    ${r.author&&r.text&&r.author!==r.text?' '+esc(r.text):''}
    <div class="meta">${esc((r.created_at||'').slice(0,10))}</div></div>`).join('');
  document.getElementById('pane').innerHTML=`<div class="dash"><h1>${label}</h1>
    <div class="sub">${rows.length.toLocaleString()} in ${esc(active)}</div>${body||'<div class="empty">Empty.</div>'}</div>`;
}
boot();
</script>
</body>
</html>
"""
