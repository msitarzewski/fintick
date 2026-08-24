"""Self-contained live dashboard and JSON feed for FinTick."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from fintick.storage import open_database

DEFAULT_LIMIT = 100
MAX_LIMIT = 250


def _json_array(value: Any) -> list[Any]:
    """Decode a stored JSON array without letting stale data break the feed."""
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _related_links(value: Any) -> list[dict[str, str]]:
    """Return only display-safe HTTP(S) research links from durable storage."""
    links: list[dict[str, str]] = []
    for item in _json_array(value):
        if not isinstance(item, dict):
            continue
        title, url, source = item.get("title"), item.get("url"), item.get("source")
        if not isinstance(title, str) or not isinstance(url, str):
            continue
        clean_title = title.strip()
        clean_url = url.strip()
        parsed = urlsplit(clean_url)
        if not clean_title or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        clean_source = source.strip() if isinstance(source, str) else parsed.netloc
        links.append({"title": clean_title, "url": clean_url, "source": clean_source})
    return links


def read_feed(database: str | Path, *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Return latest canonical posts with optional enrichment and research."""
    if limit < 1:
        raise ValueError("limit must be positive")
    limit = min(limit, MAX_LIMIT)
    connection = sqlite3.connect(database, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT p.uri, p.text, p.created_at,
                   e.status AS enrichment_status, e.summary, e.category,
                   e.importance, e.sentiment, e.instruments_json,
                   e.entities_json, e.regions_json,
                   r.status AS research_status, r.links_json
            FROM posts AS p
            LEFT JOIN enrichments AS e ON e.uri=p.uri
            LEFT JOIN research AS r ON r.uri=p.uri
            WHERE p.is_duplicate=0
            ORDER BY julianday(p.created_at) DESC, p.uri DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        connection.close()

    items: list[dict[str, Any]] = []
    for row in rows:
        complete = row["enrichment_status"] == "complete"
        items.append({
            "uri": row["uri"],
            "headline": row["text"],
            "created_at": row["created_at"],
            "enrichment_status": row["enrichment_status"] or "pending",
            "summary": row["summary"] if complete else None,
            "category": row["category"] if complete else None,
            "importance": row["importance"] if complete else None,
            "sentiment": row["sentiment"] if complete else None,
            "instruments": _json_array(row["instruments_json"]) if complete else [],
            "entities": _json_array(row["entities_json"]) if complete else [],
            "regions": _json_array(row["regions_json"]) if complete else [],
            "related": (
                _related_links(row["links_json"])
                if row["research_status"] == "complete" else []
            ),
        })
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "count": len(items),
        "items": items,
    }


DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>FinTick — Local Financial Intelligence</title>
<style>
:root {
  --night: #090c0b; --surface: #101613; --surface-2: #151d18;
  --line: #28352d; --text: #d9e4db; --muted: #91a096;
  --green: #63d391; --red: #f07d72; --amber: #e4b75c; --cyan: #71c7cf;
  --purple: #b99be8; --blue: #7ea8e5; --orange: #e29864; --slate: #9ca8a1;
}
* { box-sizing: border-box; }
html { background: var(--night); color: var(--text); font-family: "IBM Plex Mono", "SFMono-Regular", Consolas, monospace; }
body { margin: 0; min-height: 100vh; background: radial-gradient(circle at 80% 0%, #17231d 0, transparent 32rem), var(--night); }
.skip { position: absolute; left: -9999px; }
.skip:focus { left: 12px; top: 12px; z-index: 10; padding: 10px; background: var(--amber); color: var(--night); }
header { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 22px clamp(16px, 4vw, 56px) 18px; border-bottom: 1px solid var(--line); }
.brand { display: flex; align-items: baseline; gap: 14px; }
h1 { margin: 0; color: var(--amber); font-size: clamp(22px, 3vw, 32px); letter-spacing: -.06em; }
.brand span { color: var(--muted); font-size: 11px; letter-spacing: .12em; text-transform: uppercase; }
.status { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; }
.pulse { width: 8px; height: 8px; border-radius: 50%; background: var(--green); box-shadow: 0 0 10px var(--green); }
.pulse.error { background: var(--red); box-shadow: 0 0 10px var(--red); }
.tape { overflow: hidden; border-bottom: 1px solid var(--line); background: #0c110e; white-space: nowrap; }
.tape-track { display: inline-flex; min-width: max-content; animation: crawl var(--duration, 60s) linear infinite; }
.tape:hover .tape-track, .tape:focus-within .tape-track { animation-play-state: paused; }
.tape-group { display: inline-flex; }
.tick { display: inline-flex; align-items: center; gap: 10px; padding: 13px 26px 13px 0; font-size: 13px; }
.tick::before { content: "◆"; color: var(--line); margin-right: 16px; font-size: 8px; }
.tick-symbol { font-weight: 700; color: var(--amber); }
.tick-symbol.up { color: var(--green); } .tick-symbol.down { color: var(--red); }
@keyframes crawl { to { transform: translateX(-50%); } }
main { width: min(1280px, calc(100% - 32px)); margin: 0 auto; padding: 34px 0 72px; }
.feed-head { display: flex; justify-content: space-between; align-items: end; gap: 16px; margin-bottom: 18px; }
h2 { margin: 0; font-size: 15px; letter-spacing: .14em; text-transform: uppercase; }
#updated { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.feed { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 430px), 1fr)); gap: 14px; }
.card { position: relative; min-width: 0; padding: 20px; border: 1px solid var(--line); border-left: 3px solid var(--category, var(--slate)); background: linear-gradient(135deg, rgba(255,255,255,.018), transparent 50%), var(--surface); }
.card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 15px; }
.meta { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.category { color: var(--category, var(--slate)); font-size: 10px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
.time { color: var(--muted); font-size: 10px; }
.importance { letter-spacing: 2px; color: var(--amber); font-size: 10px; white-space: nowrap; }
.importance span { color: #3e493f; }
.headline { margin: 0 0 10px; color: var(--text); font: 600 clamp(15px, 1.5vw, 18px)/1.42 "IBM Plex Mono", "SFMono-Regular", Consolas, monospace; overflow-wrap: anywhere; }
.summary { margin: 0; color: #b4c1b7; font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
.pending { display: inline-block; margin-top: 10px; color: var(--muted); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
.chips { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 16px; }
.chip { display: inline-flex; gap: 6px; align-items: center; padding: 5px 8px; border: 1px solid #35443a; background: var(--surface-2); color: #cad5cc; font-size: 11px; }
.chip.up { border-color: #2d6745; color: var(--green); } .chip.down { border-color: #73433f; color: var(--red); }
.arrow { font-size: 10px; }
.sentiment { color: var(--muted); font-size: 10px; text-transform: uppercase; }
.related { margin: 17px 0 0; padding: 14px 0 0; border-top: 1px solid var(--line); list-style: none; }
.related li + li { margin-top: 8px; }
.related a { color: var(--cyan); font-size: 11px; line-height: 1.4; text-decoration: none; }
.related a:hover { text-decoration: underline; } .related a:focus-visible { outline: 2px solid var(--amber); outline-offset: 3px; }
.source { color: var(--muted); }
.empty { grid-column: 1 / -1; padding: 80px 24px; border: 1px dashed var(--line); color: var(--muted); text-align: center; line-height: 1.8; }
.error-message { color: var(--red); }
footer { padding: 20px; border-top: 1px solid var(--line); color: #66736a; text-align: center; font-size: 10px; letter-spacing: .08em; }
.cat-commodities { --category: var(--amber); } .cat-equities { --category: var(--green); }
.cat-macro { --category: var(--cyan); } .cat-central-bank { --category: var(--purple); }
.cat-geopolitics { --category: var(--red); } .cat-fx { --category: var(--blue); }
.cat-rates { --category: var(--orange); } .cat-crypto { --category: #d9c16f; }
@media (max-width: 620px) { header { align-items: flex-start; } .brand { display: block; } .brand span { display: block; margin-top: 5px; } .feed { grid-template-columns: 1fr; } }
@media (prefers-reduced-motion: reduce) { .tape-track { animation: none; } }
</style>
</head>
<body>
<a class="skip" href="#feed">Skip to live feed</a>
<header>
  <div class="brand"><h1>FinTick_</h1><span>local financial intelligence</span></div>
  <div class="status" role="status"><i class="pulse" id="pulse" aria-hidden="true"></i><span id="connection">CONNECTING</span></div>
</header>
<section class="tape" aria-label="Latest headline ticker"><div class="tape-track" id="tape"></div></section>
<main id="main">
  <div class="feed-head"><h2>Signal feed</h2><span id="updated">Awaiting feed…</span></div>
  <section class="feed" id="feed" aria-live="polite"><div class="empty">Loading the tape…</div></section>
</main>
<footer>FIN_TICK // FINANCIAL SIGNALS, LOCAL INFERENCE, ZERO CLOUD AI</footer>
<script>
'use strict';
const $ = id => document.getElementById(id);
const categoryClass = value => 'cat-' + String(value || 'other').replace(/[^a-z-]/g, '');
const safeDirection = value => ['up','down','flat'].includes(value) ? value : 'flat';
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function relativeTime(value) {
  const time = Date.parse(value); if (!Number.isFinite(time)) return 'time unknown';
  const seconds = Math.max(0, Math.round((Date.now() - time) / 1000));
  if (seconds < 60) return seconds + 's ago';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
  if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
  return Math.floor(seconds / 86400) + 'd ago';
}
function symbolLabel(instrument) {
  const direction = safeDirection(instrument.direction);
  return (direction === 'up' ? '▲ ' : direction === 'down' ? '▼ ' : '') + String(instrument.symbol || '');
}
function renderTape(items) {
  const tape = $('tape'); tape.replaceChildren();
  const visible = items.slice(0, 30);
  if (!visible.length) { tape.append(element('span', 'tick', 'No signals yet')); return; }
  function group(hidden) {
    const node = element('div', 'tape-group'); if (hidden) node.setAttribute('aria-hidden', 'true');
    for (const item of visible) {
      const tick = element('span', 'tick');
      const instrument = Array.isArray(item.instruments) ? item.instruments[0] : null;
      if (instrument && instrument.symbol) tick.append(element('b', 'tick-symbol ' + safeDirection(instrument.direction), symbolLabel(instrument)));
      tick.append(document.createTextNode(String(item.headline || ''))); node.append(tick);
    }
    return node;
  }
  tape.append(group(false), group(true));
  tape.style.setProperty('--duration', Math.max(35, visible.length * 7) + 's');
}
function renderCard(item) {
  const card = element('article', 'card ' + categoryClass(item.category));
  const head = element('div', 'card-head'), meta = element('div', 'meta');
  meta.append(element('span', 'category', item.category || 'raw signal'), element('time', 'time', relativeTime(item.created_at)));
  head.append(meta);
  if (Number.isInteger(item.importance)) {
    const stars = element('span', 'importance'); stars.setAttribute('aria-label', 'Importance ' + item.importance + ' of 5');
    stars.append(document.createTextNode('◆'.repeat(item.importance)), element('span', '', '◆'.repeat(5 - item.importance))); head.append(stars);
  }
  card.append(head, element('h3', 'headline', item.headline || 'Untitled signal'));
  if (item.summary) card.append(element('p', 'summary', item.summary));
  else card.append(element('span', 'pending', item.enrichment_status === 'error' ? 'Enrichment retry queued' : 'Raw signal · analysis pending'));
  const instruments = Array.isArray(item.instruments) ? item.instruments : [];
  if (instruments.length || item.sentiment) {
    const chips = element('div', 'chips');
    for (const instrument of instruments) {
      const direction = safeDirection(instrument.direction), chip = element('span', 'chip ' + direction);
      chip.title = String(instrument.name || instrument.symbol || 'Instrument');
      chip.append(element('span', 'arrow', direction === 'up' ? '▲' : direction === 'down' ? '▼' : '—'), document.createTextNode(String(instrument.symbol || ''))); chips.append(chip);
    }
    if (item.sentiment) chips.append(element('span', 'sentiment', String(item.sentiment)));
    card.append(chips);
  }
  const links = Array.isArray(item.related) ? item.related : [];
  if (links.length) {
    const list = element('ul', 'related');
    for (const story of links) {
      if (!story || !story.url) continue;
      const li = element('li'), link = element('a', '', String(story.title || story.url));
      link.href = String(story.url); link.target = '_blank'; link.rel = 'noopener noreferrer';
      li.append(link); if (story.source) li.append(document.createTextNode(' '), element('span', 'source', '— ' + story.source)); list.append(li);
    }
    if (list.childNodes.length) card.append(list);
  }
  return card;
}
function render(data) {
  const items = Array.isArray(data.items) ? data.items : [], feed = $('feed');
  renderTape(items); feed.replaceChildren();
  if (!items.length) feed.append(element('div', 'empty', 'The tape is quiet. Ingested signals will appear here automatically.'));
  else for (const item of items) feed.append(renderCard(item));
  const generated = new Date(data.generated_at);
  $('updated').textContent = Number.isNaN(generated.valueOf()) ? items.length + ' signals' : items.length + ' signals · updated ' + generated.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
  $('connection').textContent = 'LIVE'; $('pulse').classList.remove('error');
}
let refreshInFlight = false;
async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const response = await fetch('/api/feed', {cache: 'no-store'}); if (!response.ok) throw new Error('HTTP ' + response.status);
    render(await response.json());
  } catch (error) {
    $('connection').textContent = 'RECONNECTING'; $('pulse').classList.add('error');
    if (!$('feed').querySelector('.card')) { const msg = element('div', 'empty error-message', 'Feed unavailable. FinTick will retry automatically.'); $('feed').replaceChildren(msg); }
  } finally { refreshInFlight = false; }
}
refresh(); setInterval(refresh, 20000);
</script>
</body>
</html>'''


class DashboardServer(ThreadingHTTPServer):
    """HTTP server carrying the database path used by request handlers."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], database: str | Path) -> None:
        self.database = Path(database)
        # Run migrations once before accepting concurrent read requests.
        with open_database(self.database):
            pass
        super().__init__(address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parts = urlsplit(self.path)
        if parts.path in {"/", "/index.html"}:
            self._send(HTTPStatus.OK, DASHBOARD_HTML.encode(), "text/html; charset=utf-8")
            return
        if parts.path == "/api/feed":
            raw_limit = parse_qs(parts.query).get("limit", [str(DEFAULT_LIMIT)])[0]
            try:
                limit = int(raw_limit)
                server = cast(DashboardServer, self.server)
                payload = read_feed(server.database, limit=limit)
            except ValueError as error:
                self._send(HTTPStatus.BAD_REQUEST, json.dumps({"error": str(error)}).encode(), "application/json")
                return
            except (OSError, sqlite3.Error) as error:
                self.log_error("feed read failed: %s", error)
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b'{"error":"feed unavailable"}', "application/json")
                return
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
            return
        self._send(HTTPStatus.NOT_FOUND, b'{"error":"not found"}', "application/json")

    def log_message(self, format: str, *args: Any) -> None:
        print(f"dashboard {self.address_string()} {format % args}", flush=True)


def serve_dashboard(database: str | Path, *, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Serve the dashboard until interrupted."""
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    server = DashboardServer((host, port), database)
    actual_host, actual_port = server.server_address[:2]
    print(f"FinTick dashboard listening on http://{actual_host}:{actual_port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
