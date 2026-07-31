"""Generate a self-contained, offline HTML viewer of the archive.

One HTML file, no server, no internet, no dependency on this project continuing to
exist. Records are embedded as JSON and browsed/searched entirely client-side. This
is the "your data is actually yours and usable" promise made concrete.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from .store.archive import Archive

_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Save Your Shit — your archive</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0; background: #0e0f13; color: #e7e9ee; }}
  header {{ padding: 20px 24px; border-bottom: 1px solid #22242c; position: sticky; top: 0;
           background: #0e0f13; z-index: 5; }}
  h1 {{ margin: 0 0 4px; font-size: 20px; }}
  .meta {{ color: #9aa0ad; font-size: 13px; }}
  .controls {{ display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }}
  input, select {{ padding: 8px 10px; border-radius: 8px; border: 1px solid #2a2d37;
                   background: #151721; color: #e7e9ee; font-size: 14px; }}
  input {{ flex: 1; min-width: 200px; }}
  main {{ padding: 16px 24px 80px; max-width: 900px; margin: 0 auto; }}
  .rec {{ padding: 10px 12px; border: 1px solid #1d1f28; border-radius: 10px;
          margin: 8px 0; background: #12141c; }}
  .rec .top {{ display: flex; gap: 8px; align-items: baseline; font-size: 12px; color: #8b90a0; }}
  .badge {{ background: #1f2430; color: #b9c0d0; border-radius: 6px; padding: 1px 6px; font-size: 11px; }}
  .rec .text {{ margin-top: 4px; white-space: pre-wrap; word-break: break-word; }}
  .who {{ color: #cfd4e0; font-weight: 600; }}
  .empty {{ color: #8b90a0; text-align: center; padding: 40px; }}
  footer {{ color: #6b7080; font-size: 12px; text-align: center; padding: 24px; }}
</style>
</head>
<body>
<header>
  <h1>🛟 Your archive</h1>
  <div class="meta">{count} items · generated offline · nothing here left your machine</div>
  <div class="controls">
    <input id="q" placeholder="Search your chats, posts, followers…" autofocus>
    <select id="connector"><option value="">All platforms</option>{options}</select>
    <select id="type"><option value="">All types</option>{types}</select>
  </div>
</header>
<main id="list"></main>
<footer>Save Your Shit — your data, forever, offline.</footer>
<script>
const DATA = {data};
const list = document.getElementById('list');
const q = document.getElementById('q');
const connSel = document.getElementById('connector');
const typeSel = document.getElementById('type');
function esc(s) {{ const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }}
function render() {{
  const term = q.value.toLowerCase();
  const conn = connSel.value, type = typeSel.value;
  const rows = DATA.filter(r =>
    (!conn || r.connector === conn) &&
    (!type || r.type === type) &&
    (!term || (r.text||'').toLowerCase().includes(term) ||
              (r.author||'').toLowerCase().includes(term) ||
              (r.thread||'').toLowerCase().includes(term))
  ).slice(0, 2000);
  if (!rows.length) {{ list.innerHTML = '<div class="empty">No matches.</div>'; return; }}
  list.innerHTML = rows.map(r => `
    <div class="rec">
      <div class="top">
        <span class="badge">${{esc(r.connector)}}</span>
        <span class="badge">${{esc(r.type)}}</span>
        <span>${{esc((r.created_at||'').slice(0,19).replace('T',' '))}}</span>
        ${{r.thread ? '<span>· '+esc(r.thread)+'</span>' : ''}}
      </div>
      <div class="text"><span class="who">${{esc(r.author||'')}}</span> ${{esc(r.text||'')}}</div>
    </div>`).join('');
}}
[q, connSel, typeSel].forEach(el => el.addEventListener('input', render));
render();
</script>
</body>
</html>
"""


def _safe_json(obj) -> str:
    """JSON for embedding inside a <script> tag.

    ``json.dumps`` does not escape ``<``, so a ``</script>`` inside any message
    would break out of the tag (and could inject markup). Escaping ``<>&`` to
    unicode escapes makes the payload inert while remaining valid JSON.
    """
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def build_viewer(archive: Archive, out_path: Path, *, limit: int | None = 20000) -> Path:
    records = archive.index.iter_records(limit=limit)
    connectors = sorted({r["connector"] for r in records})
    types = sorted({r["type"] for r in records})
    options = "".join(
        f'<option value="{html.escape(c)}">{html.escape(c)}</option>' for c in connectors
    )
    type_opts = "".join(
        f'<option value="{html.escape(t)}">{html.escape(t)}</option>' for t in types
    )
    doc = _TEMPLATE.format(
        count=len(records),
        options=options,
        types=type_opts,
        data=_safe_json(records),
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    return out_path
