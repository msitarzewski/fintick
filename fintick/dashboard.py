"""Self-contained v2 event board and JSON API for FinTick."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from fintick.service_handoff import database_identity
from fintick.storage import load_events, load_pipeline_health, open_database

DEFAULT_LIMIT = 100
MAX_LIMIT = 250
STATUS_ORDER = {"breaking": 0, "contradicted": 1, "developing": 2, "confirmed": 3}


def _safe_validations(value: Any) -> list[dict[str, Any]]:
    """Keep only display-safe HTTP(S) external stories."""
    if not isinstance(value, list):
        return []
    safe: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        feed_url = item.get("feed_url")
        parsed_feed = urlsplit(feed_url) if isinstance(feed_url, str) else None
        safe_feed_url = (
            feed_url
            if parsed_feed
            and parsed_feed.scheme in {"http", "https"}
            and parsed_feed.netloc
            else None
        )
        safe.append({
            "url": url,
            "title": item.get("title") if isinstance(item.get("title"), str) else url,
            "publisher": (
                item.get("publisher")
                if isinstance(item.get("publisher"), str)
                else parsed.netloc.removeprefix("www.")
            ),
            "stance": item.get("stance"),
            "published_at": item.get("published_at"),
            "feed_name": item.get("feed_name") if isinstance(item.get("feed_name"), str) else None,
            "feed_url": safe_feed_url,
            "feed_type": (
                item.get("feed_type")
                if item.get("feed_type") in {"rss", "atom", "json"}
                else None
            ),
        })
    return safe


def read_feed(database: str | Path, *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Return validation-prioritized event cards for the board."""
    if limit < 1:
        raise ValueError("limit must be positive")
    limit = min(limit, MAX_LIMIT)
    with open_database(database) as connection:
        events = load_events(connection, limit=None)
        pipeline = load_pipeline_health(connection)
    pipeline["database_identity"] = database_identity(database)
    for event in events:
        event["validations"] = _safe_validations(event.get("validations"))
    events.sort(key=lambda event: (
        STATUS_ORDER.get(str(event.get("status")), 9),
        str(event.get("first_seen_at", "")),
        int(event.get("id", 0)),
    ))
    # Keep each status group newest-first without letting recency outrank urgency.
    ordered: list[dict[str, Any]] = []
    for status in ("breaking", "contradicted", "developing", "confirmed"):
        group = [event for event in events if event.get("status") == status]
        group.sort(key=lambda event: (str(event.get("first_seen_at", "")), int(event["id"])), reverse=True)
        ordered.extend(group)
    ordered.extend(event for event in events if event.get("status") not in STATUS_ORDER)
    items = ordered[:limit]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "count": len(items),
        "pipeline": pipeline,
        "items": items,
    }


DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>FinTick — The Edge Board</title>
<style>
:root {
  --night:#0b0d0c; --surface:#121513; --lift:#181c19; --line:#303630;
  --text:#e2e7e3; --muted:#98a29b; --dim:#687169;
  --breaking:#ff6b61; --confirmed:#62d391; --developing:#e7b95b;
  --contradicted:#cf8cff; --cyan:#75cbd0; --amber:#e0a765;
}
*{box-sizing:border-box} html{background:var(--night);color:var(--text);font-family:"IBM Plex Mono","SFMono-Regular",Consolas,monospace}
body{margin:0;min-height:100vh;background:radial-gradient(circle at 12% -10%,#252017 0,transparent 31rem),radial-gradient(circle at 90% 10%,#13231b 0,transparent 35rem),var(--night)}
.skip{position:absolute;left:-9999px}.skip:focus{left:16px;top:16px;z-index:5;background:var(--amber);color:var(--night);padding:12px}
header{padding:24px clamp(18px,4vw,60px) 18px;border-bottom:1px solid var(--line)}
.header-row{display:flex;align-items:flex-start;justify-content:space-between;gap:24px}.brand{display:flex;align-items:baseline;gap:15px}
h1{margin:0;color:var(--amber);font-size:clamp(24px,3vw,34px);letter-spacing:-.07em}.brand span{font-size:10px;letter-spacing:.17em;text-transform:uppercase;color:var(--muted)}
.connection{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:11px;letter-spacing:.12em}.pulse{width:8px;height:8px;border-radius:50%;background:var(--confirmed);box-shadow:0 0 12px var(--confirmed)}.pulse.catchup{background:var(--developing);box-shadow:0 0 12px var(--developing)}.pulse.error{background:var(--breaking);box-shadow:0 0 12px var(--breaking)}
.pipeline-health{display:flex;flex-wrap:wrap;gap:8px 20px;margin-top:18px;padding:10px 12px;border:1px solid var(--line);background:rgba(0,0,0,.16);color:var(--muted);font-size:9px;letter-spacing:.1em;text-transform:uppercase}.pipeline-health b{color:var(--text);font-weight:650}.pipeline-health .good b{color:var(--confirmed)}.pipeline-health .warn b{color:var(--developing)}.pipeline-health .bad b{color:var(--breaking)}
.metrics{display:flex;flex-wrap:wrap;gap:20px;margin-top:24px}.metric{min-width:108px}.metric b{display:block;font-size:22px;line-height:1.1}.metric span{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.13em}.metric.breaking b{color:var(--breaking)}
.tape{overflow:hidden;border-bottom:1px solid var(--line);background:#0e110f;white-space:nowrap}.tape-track{display:inline-flex;min-width:max-content;animation:crawl var(--duration,50s) linear infinite}.tape:hover .tape-track{animation-play-state:paused}.tape-group{display:inline-flex}.tick{padding:11px 28px 11px 0;font-size:11px;color:var(--muted)}.tick::before{content:"◆";color:var(--line);margin:0 18px}.tick.breaking{color:var(--breaking)}.tick.confirmed{color:var(--confirmed)}
@keyframes crawl{to{transform:translateX(-50%)}}
main{width:min(1320px,calc(100% - 32px));margin:0 auto;padding:38px 0 76px}.board-head{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:18px}.board-head h2{margin:0;font-size:15px;letter-spacing:.15em;text-transform:uppercase}.board-head p{margin:7px 0 0;color:var(--muted);font-size:11px}.updated{font-size:10px;color:var(--dim);font-variant-numeric:tabular-nums}
.feed{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,440px),1fr));gap:16px}.card{position:relative;min-width:0;padding:22px;border:1px solid var(--line);border-top:3px solid var(--state);background:linear-gradient(145deg,rgba(255,255,255,.025),transparent 45%),var(--surface)}.card.breaking{--state:var(--breaking);background:linear-gradient(145deg,rgba(255,107,97,.10),transparent 48%),var(--surface);box-shadow:0 0 0 1px rgba(255,107,97,.09),0 15px 50px rgba(0,0,0,.22)}.card.confirmed{--state:var(--confirmed)}.card.developing{--state:var(--developing)}.card.contradicted{--state:var(--contradicted)}
.badge{display:inline-flex;align-items:center;min-height:32px;padding:7px 10px;border:1px solid color-mix(in srgb,var(--state) 55%,transparent);background:color-mix(in srgb,var(--state) 11%,transparent);color:var(--state);font-size:10px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}.badge::before{content:"";width:7px;height:7px;border-radius:50%;margin-right:8px;background:var(--state);box-shadow:0 0 9px var(--state)}
.card-meta{display:flex;justify-content:space-between;gap:16px;align-items:start;margin-bottom:16px}.importance{color:var(--amber);font-size:9px;letter-spacing:2px}.importance i{color:#424941;font-style:normal}.headline{margin:0 0 10px;font:650 clamp(17px,1.7vw,21px)/1.38 "IBM Plex Mono","SFMono-Regular",Consolas,monospace;overflow-wrap:anywhere}.summary{margin:0;color:#bcc5be;font-size:13px;line-height:1.6}.origin{margin-top:14px;color:var(--muted);font-size:10px;letter-spacing:.04em}.origin strong{color:var(--text)}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:8px;margin-top:18px}.fact{padding:10px;border:1px solid #303831;background:var(--lift)}.fact b{display:block;color:var(--text);font-size:14px;margin-bottom:3px}.fact span{color:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:.08em}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:14px}.chip{padding:5px 8px;border:1px solid #384039;background:var(--lift);font-size:10px}.chip.up{color:var(--confirmed);border-color:#356347}.chip.down{color:var(--breaking);border-color:#6c403d}
.sources{margin:18px 0 0;padding:15px 0 0;border-top:1px solid var(--line);list-style:none}.sources li+li{margin-top:9px}.sources a{color:var(--cyan);font-size:11px;line-height:1.45;text-decoration:underline;text-decoration-color:rgba(117,203,208,.35);text-underline-offset:3px}.sources a:focus-visible{outline:2px solid var(--amber);outline-offset:3px}.publisher{color:var(--muted);font-size:10px}.lag{margin-top:12px;color:var(--confirmed);font-size:10px}.empty{grid-column:1/-1;padding:90px 24px;border:1px dashed var(--line);color:var(--muted);text-align:center;line-height:1.8}.error-message{color:var(--breaking)}
footer{padding:20px;border-top:1px solid var(--line);color:var(--dim);text-align:center;font-size:9px;letter-spacing:.1em}
@media(max-width:640px){.header-row{align-items:flex-start}.board-head{align-items:flex-start;flex-direction:column;gap:10px}.brand{display:block}.brand span{display:block;margin-top:6px}.feed{grid-template-columns:1fr}.metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.metric{min-width:0}}
@media(prefers-reduced-motion:reduce){.tape-track{animation:none}}
</style>
</head>
<body>
<a class="skip" href="#feed">Skip to event board</a>
<header>
  <div class="header-row"><div class="brand"><h1>FinTick_</h1><span>ahead of the wire</span></div><div class="connection" role="status"><i class="pulse" id="pulse" aria-hidden="true"></i><span id="connection">CONNECTING</span></div></div>
  <div class="pipeline-health" id="pipeline-health" aria-label="Pipeline accounting">Awaiting pipeline health…</div>
  <div class="metrics" id="metrics" aria-label="Event status summary"></div>
</header>
<section class="tape" aria-label="Latest event ticker"><div class="tape-track" id="tape"></div></section>
<main>
  <div class="board-head"><div><h2>The Edge Board</h2><p>What the stream caught—and whether the news has caught up.</p></div><span class="updated" id="updated">Awaiting events…</span></div>
  <section class="feed" id="feed" aria-live="polite"><div class="empty">Loading the edge…</div></section>
</main>
<footer>ONE STREAM // DISTINCT EVENTS // EXTERNAL VALIDATION // LOCAL INFERENCE</footer>
<script>
'use strict';
const $=id=>document.getElementById(id);
function element(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node}
function relativeTime(value){const time=Date.parse(value);if(!Number.isFinite(time))return'time unknown';const s=Math.max(0,Math.round((Date.now()-time)/1000));if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago'}
function safeStatus(value){return['breaking','confirmed','contradicted','developing'].includes(value)?value:'developing'}
function badgeText(item){const n=Array.isArray(item.validations)?item.validations.length:0;switch(safeStatus(item.status)){case'breaking':return'BREAKING — no corroboration yet';case'confirmed':return'CONFIRMED — '+n+' source'+(n===1?'':'s');case'contradicted':return'CONTRADICTED';default:return'DEVELOPING'}}
function lagText(seconds){if(!Number.isFinite(seconds))return'';const abs=Math.abs(seconds),value=abs<3600?Math.round(abs/60)+' min':(abs/3600).toFixed(1)+' hr';return seconds>=0?'news +'+value+' after the stream':'news '+value+' before the stream'}
function renderPipeline(value){const p=value&&typeof value==='object'?value:{},node=$('pipeline-health'),backlog=Number(p.backlog)||0,errors=Number(p.terminal_errors)||0,accounted=Number(p.accounted)||0,posts=Number(p.posts)||0;node.replaceChildren();let state='CAUGHT UP',tone='good';if(errors>0){state='TERMINAL ERRORS';tone='bad'}else if(backlog>0){state='CATCHING UP';tone='warn'}const status=element('span',tone);status.append(element('b','',state));node.append(status);const coverage=element('span','');coverage.append(document.createTextNode('accounted '),element('b','',accounted+' / '+posts));node.append(coverage);const queue=element('span',backlog?'warn':'good');queue.append(document.createTextNode('backlog '),element('b','',String(backlog)));node.append(queue);if(backlog&&p.oldest_pending_at){const oldest=element('span','');oldest.append(document.createTextNode('oldest '),element('b','',relativeTime(p.oldest_pending_at)));node.append(oldest)}if(errors){const terminal=element('span','bad');terminal.append(document.createTextNode('errors '),element('b','',String(errors)));node.append(terminal)}$('connection').textContent=state;$('pulse').classList.toggle('catchup',backlog>0&&!errors);$('pulse').classList.toggle('error',errors>0)}
function renderMetrics(items){const metrics=$('metrics');metrics.replaceChildren();for(const [status,label] of [['breaking','breaking now'],['confirmed','confirmed'],['developing','developing'],['contradicted','contradicted']]){const box=element('div','metric '+status);box.append(element('b','',String(items.filter(x=>x.status===status).length)),element('span','',label));metrics.append(box)}}
function renderTape(items){const tape=$('tape');tape.replaceChildren();if(!items.length){tape.append(element('span','tick','No events yet'));return}function group(hidden){const node=element('div','tape-group');if(hidden)node.setAttribute('aria-hidden','true');for(const item of items.slice(0,24))node.append(element('span','tick '+safeStatus(item.status),String(item.headline||'')));return node}tape.append(group(false),group(true));tape.style.setProperty('--duration',Math.max(38,items.length*8)+'s')}
function renderCard(item){const status=safeStatus(item.status),card=element('article','card '+status);const meta=element('div','card-meta');meta.append(element('span','badge',badgeText(item)));if(Number.isInteger(item.importance)){const rank=element('span','importance');rank.setAttribute('aria-label','Importance '+item.importance+' of 5');rank.append(document.createTextNode('◆'.repeat(item.importance)),element('i','', '◆'.repeat(5-item.importance)));meta.append(rank)}card.append(meta,element('h3','headline',String(item.headline||'Untitled event')));if(item.summary)card.append(element('p','summary',String(item.summary)));const origin=element('p','origin');origin.append(document.createTextNode('via the stream · seen '),element('strong','',String(item.stream_seen||0)+'×'),document.createTextNode(' · '+relativeTime(item.first_seen_at)));card.append(origin);
const facts=Array.isArray(item.facts)?item.facts:[];if(facts.length){const grid=element('div','facts');for(const fact of facts){if(!fact||fact.value===undefined)continue;const node=element('div','fact');node.append(element('b','',String(fact.value)+(fact.unit?' '+fact.unit:'')),element('span','',String(fact.label||'fact')));grid.append(node)}if(grid.childNodes.length)card.append(grid)}
const instruments=Array.isArray(item.instruments)?item.instruments:[];if(instruments.length){const chips=element('div','chips');for(const instrument of instruments){const direction=['up','down','flat'].includes(instrument.direction)?instrument.direction:'flat';const marker=direction==='up'?'▲ ':direction==='down'?'▼ ':'— ';const chip=element('span','chip '+direction,marker+String(instrument.symbol||instrument.name||''));chip.title=String(instrument.name||instrument.symbol||'Instrument');chips.append(chip)}card.append(chips)}
const links=Array.isArray(item.validations)?item.validations:[];if(links.length){const list=element('ul','sources');for(const story of links){if(!story||!story.url)continue;const li=element('li'),link=element('a','',String(story.title||story.url));link.href=String(story.url);link.target='_blank';link.rel='noopener noreferrer';li.append(link,document.createTextNode(' '),element('span','publisher','— '+String(story.publisher||'external news')));list.append(li)}if(list.childNodes.length)card.append(list)}const lag=lagText(item.lead_seconds);if(lag)card.append(element('p','lag',lag));return card}
function render(data){const items=Array.isArray(data.items)?data.items:[],feed=$('feed');renderPipeline(data.pipeline);renderMetrics(items);renderTape(items);feed.replaceChildren();if(!items.length)feed.append(element('div','empty','No aggregated events yet. Ingest the stream, then run FinTick aggregate.'));else for(const item of items)feed.append(renderCard(item));const generated=new Date(data.generated_at);$('updated').textContent=Number.isNaN(generated.valueOf())?items.length+' events':items.length+' events · updated '+generated.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}
let refreshInFlight=false;async function refresh(){if(refreshInFlight)return;refreshInFlight=true;try{const response=await fetch('/api/feed',{cache:'no-store'});if(!response.ok)throw new Error('HTTP '+response.status);render(await response.json())}catch(error){$('connection').textContent='RECONNECTING';$('pulse').classList.add('error');if(!$('feed').querySelector('.card'))$('feed').replaceChildren(element('div','empty error-message','Event feed unavailable. FinTick will retry automatically.'))}finally{refreshInFlight=false}}
refresh(); setInterval(refresh, 20000);
</script>
</body>
</html>'''


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], database: str | Path) -> None:
        self.database = Path(database)
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
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path in {"/", "/index.html"}:
            self._send(HTTPStatus.OK, DASHBOARD_HTML.encode(), "text/html; charset=utf-8")
            return
        if parts.path == "/api/feed":
            raw_limit = parse_qs(parts.query).get("limit", [str(DEFAULT_LIMIT)])[0]
            try:
                server = cast(DashboardServer, self.server)
                payload = read_feed(server.database, limit=int(raw_limit))
            except ValueError as error:
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    json.dumps({"error": str(error)}).encode(),
                    "application/json",
                )
                return
            except (OSError, sqlite3.Error) as error:
                self.log_error("feed read failed: %s", error)
                self._send(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    b'{"error":"feed unavailable"}',
                    "application/json",
                )
                return
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
            return
        self._send(HTTPStatus.NOT_FOUND, b'{"error":"not found"}', "application/json")

    def log_message(self, format: str, *args: Any) -> None:
        print(f"dashboard {self.address_string()} {format % args}", flush=True)


def serve_dashboard(database: str | Path, *, host: str = "127.0.0.1", port: int = 8137) -> None:
    """Serve the event board until interrupted."""
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
