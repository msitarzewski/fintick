"""Self-contained v2 event board and JSON API for FinTick."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from fintick.aggregate import inference_cost_usd
from fintick.service_handoff import database_identity
from fintick.storage import (
    load_events,
    load_inference_usage,
    load_pipeline_health,
    open_database,
)

ASSET_DIR = Path(__file__).resolve().parent / "assets"
ASSET_ROUTES = {
    "/og.png": ("og.png", "image/png"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.ico": ("favicon.svg", "image/svg+xml"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}

DEFAULT_LIMIT = 100
MAX_LIMIT = 250
# 'unconfirmed' sinks to the bottom: it is breaking that the wire never caught up on.
STATUS_ORDER = {
    "breaking": 0, "contradicted": 1, "developing": 2, "confirmed": 3, "unconfirmed": 4,
}
# A breaking event ages into 'unconfirmed' once the wire has had this long to catch up
# and still hasn't. Purely a passage-of-time lens over the stored validation fact
# (breaking = no corroboration found), so the flip needs no re-hunt and no DB write.
BREAKING_TTL_SECONDS = int(os.environ.get("FINTICK_BREAKING_TTL_SECONDS", "3600"))


def _effective_status(status: Any, first_seen_at: Any, now: datetime) -> str:
    """Derive the display status: breaking past its TTL reads as 'unconfirmed'."""
    if status != "breaking":
        return str(status)
    try:
        first = datetime.fromisoformat(str(first_seen_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "breaking"
    if first.tzinfo is None:
        return "breaking"
    age = (now - first.astimezone(UTC)).total_seconds()
    return "unconfirmed" if age >= BREAKING_TTL_SECONDS else "breaking"


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
        usage = load_inference_usage(connection)
    pipeline["database_identity"] = database_identity(database)
    # Operator cost tracker: price each window's per-model token sums (see ?ops).
    pipeline["cost"] = {
        label: {
            "usd": round(sum(
                inference_cost_usd(
                    row["model"], row["prompt_tokens"], row["completion_tokens"]
                ) for row in rows
            ), 4),
            "calls": sum(row["calls"] for row in rows),
        }
        for label, rows in usage.items()
    }
    now = datetime.now(UTC)
    for event in events:
        event["validations"] = _safe_validations(event.get("validations"))
        event["status"] = _effective_status(
            event.get("status"), event.get("first_seen_at"), now
        )
    events.sort(key=lambda event: (
        STATUS_ORDER.get(str(event.get("status")), 9),
        str(event.get("first_seen_at", "")),
        int(event.get("id", 0)),
    ))
    # Keep each status group newest-first without letting recency outrank urgency.
    ordered: list[dict[str, Any]] = []
    for status in ("breaking", "contradicted", "developing", "confirmed", "unconfirmed"):
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


SITE_ORIGIN = os.environ.get("FINTICK_SITE_ORIGIN", "https://fintick.fyi").rstrip("/")

# Crawlable: the board itself. Not crawlable: the JSON API, which carries no prose to
# index.
#
# The operator view is deliberately NOT named here. robots.txt is a public file that
# scanners fetch first, so a Disallow line advertises a path rather than protecting it.
# Consolidating the operator view onto the board is the canonical link's job, and it
# does that without publishing anything.
ROBOTS_TXT = """User-agent: *
Allow: /$
Disallow: /api/

Sitemap: {origin}/sitemap.xml
"""

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{origin}/</loc>
    <changefreq>hourly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
"""

# AEO: a plain-language brief for answer engines and browsing agents, which reward a
# stated method and explicit limits over marketing copy.
LLMS_TXT = """# FinTick

> A live tape of financial events, scored by whether independent news has caught up yet.

FinTick ingests one public financial stream, aggregates its posts into distinct events
using a language model, extracts structured facts from each, then searches independent
news sources to corroborate them.

## How an event is classified

- **breaking** — no independent source has reported it yet. The stream is ahead of the wire.
- **unconfirmed** — was breaking, and the wire still had not corroborated it after the
  configured window elapsed.
- **developing** — partially corroborated.
- **confirmed** — independently reported by one or more outlets.
- **contradicted** — an independent source disputes it.

## Method and limits

- Aggregation performs the deduplication: repeated posts about one event merge into that
  event and are retained as `seen N times` evidence rather than discarded.
- Every ingested post is preserved by immutable URI and receives exactly one durable
  outcome. A post belongs to at most one event.
- Social posts are treated as stream signal, never as independent corroboration.
- FinTick reports whether an event has been corroborated. It does not assert that an
  event is true, and it is not investment advice.

## Endpoints

- `/` — the board.
- `/api/feed` — the same events as JSON.

## Source

- https://github.com/msitarzewski/fintick
"""

DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#0b0d0c" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="#f3f1ea" media="(prefers-color-scheme: light)">
<title>FinTick — The Edge Board</title>
<meta name="description" content="A live tape of financial events. FinTick aggregates a public stream into distinct events, extracts the facts, then hunts independent news to corroborate them — an event no outlet has confirmed yet is flagged breaking.">
<link rel="canonical" href="https://fintick.fyi/">
<meta name="robots" content="index,follow,max-image-preview:large,max-snippet:-1">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta property="og:type" content="website">
<meta property="og:site_name" content="FinTick">
<meta property="og:title" content="FinTick — ahead of the wire">
<meta property="og:description" content="A live tape of financial events, scored by whether independent news has caught up yet.">
<meta property="og:url" content="https://fintick.fyi/">
<meta property="og:image" content="https://fintick.fyi/og.png">
<meta property="og:image:type" content="image/png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="FinTick — a breaking financial event with zero external sources, ahead of the wire.">
<meta property="og:locale" content="en_US">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="FinTick — ahead of the wire">
<meta name="twitter:description" content="A live tape of financial events, scored by whether independent news has caught up yet.">
<meta name="twitter:image" content="https://fintick.fyi/og.png">
<meta name="twitter:image:alt" content="FinTick — a breaking financial event with zero external sources, ahead of the wire.">
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
{"@type":"WebSite","@id":"https://fintick.fyi/#website","url":"https://fintick.fyi/","name":"FinTick",
 "description":"A live tape of financial events, scored by whether independent news has caught up yet.",
 "inLanguage":"en"},
{"@type":"WebApplication","@id":"https://fintick.fyi/#app","url":"https://fintick.fyi/","name":"FinTick",
 "applicationCategory":"FinanceApplication","browserRequirements":"Requires JavaScript.",
 "operatingSystem":"Any","isAccessibleForFree":true,
 "offers":{"@type":"Offer","price":"0","priceCurrency":"USD"},
 "description":"FinTick ingests a public financial stream, aggregates posts into distinct events, extracts structured facts, and searches independent news to corroborate each one. An event with no external sources is flagged breaking.",
 "featureList":["Event aggregation from a public stream","Structured fact extraction","Independent news corroboration","Breaking detection when no outlet has reported an event yet"]}
]}
</script>
<script>try{var t=localStorage.getItem('fintick-theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}
/* Operator telemetry (pipeline health + status pill) is hidden for public visitors.
   Reveal it with ?ops (or ?ops=1) — it persists per-browser; ?ops=0 clears it. */
try{var q=new URLSearchParams(location.search),op;if(q.has('ops')){op=q.get('ops')!=='0';localStorage.setItem('fintick-ops',op?'1':'0')}else{op=localStorage.getItem('fintick-ops')==='1'}if(op)document.documentElement.dataset.ops='1'}catch(e){}</script>
<style>
:root {
  --night:#0b0d0c; --surface:#121513; --lift:#181c19; --line:#303630;
  --text:#e2e7e3; --muted:#98a29b; --dim:#687169;
  --breaking:#ff6b61; --confirmed:#62d391; --developing:#e7b95b;
  --contradicted:#cf8cff; --cyan:#75cbd0; --amber:#e0a765; --unconfirmed:#8f9aa0;
  --glow1:#252017; --glow2:#13231b; --panel:#0e110f; --pill:rgba(0,0,0,.16);
  --shadow:rgba(0,0,0,.22); --inset-line:#303831; --card-sheen:rgba(255,255,255,.025);
}
/* Light palette — status hues darkened for contrast on paper. */
:root[data-theme="light"] {
  --night:#f3f1ea; --surface:#ffffff; --lift:#eeeae1; --line:#d7d2c4;
  --text:#1a1f1b; --muted:#59625a; --dim:#8b938b;
  --breaking:#cf3327; --confirmed:#1c9a53; --developing:#9c6f16;
  --contradicted:#8a44c6; --cyan:#1c848a; --amber:#a76a17; --unconfirmed:#5f6870;
  --glow1:#efe8d7; --glow2:#e1ece4; --panel:#eeeae1; --pill:rgba(0,0,0,.035);
  --shadow:rgba(60,55,40,.12); --inset-line:#e2ded1; --card-sheen:rgba(0,0,0,.015);
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    --night:#f3f1ea; --surface:#ffffff; --lift:#eeeae1; --line:#d7d2c4;
    --text:#1a1f1b; --muted:#59625a; --dim:#8b938b;
    --breaking:#cf3327; --confirmed:#1c9a53; --developing:#9c6f16;
    --contradicted:#8a44c6; --cyan:#1c848a; --amber:#a76a17; --unconfirmed:#5f6870;
    --glow1:#efe8d7; --glow2:#e1ece4; --panel:#eeeae1; --pill:rgba(0,0,0,.035);
    --shadow:rgba(60,55,40,.12); --inset-line:#e2ded1; --card-sheen:rgba(0,0,0,.015);
  }
}
*{box-sizing:border-box} html{background:var(--night);color:var(--text);font-family:"IBM Plex Mono","SFMono-Regular",Consolas,monospace}
body{margin:0;min-height:100vh;background:radial-gradient(circle at 12% -10%,var(--glow1) 0,transparent 31rem),radial-gradient(circle at 90% 10%,var(--glow2) 0,transparent 35rem),var(--night);transition:background-color .2s,color .2s}
.skip{position:absolute;left:-9999px}.skip:focus{left:16px;top:16px;z-index:5;background:var(--amber);color:var(--night);padding:12px}
header{position:sticky;top:0;z-index:30;padding:15px clamp(18px,4vw,60px);border-bottom:1px solid var(--line);background:color-mix(in srgb,var(--night) 72%,transparent);-webkit-backdrop-filter:blur(16px) saturate(1.4);backdrop-filter:blur(16px) saturate(1.4)}
.topnav{display:flex;align-items:center;flex-wrap:wrap;justify-content:space-between;gap:12px 20px}.brand{display:flex;align-items:baseline;gap:15px;flex:0 0 auto}
h1{margin:0;color:var(--amber);font-size:clamp(24px,3vw,34px);letter-spacing:-.07em}.brand span{font-size:10px;letter-spacing:.17em;text-transform:uppercase;color:var(--muted)}
.connection{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:11px;letter-spacing:.12em}.pulse{width:8px;height:8px;border-radius:50%;background:var(--confirmed);box-shadow:0 0 12px var(--confirmed)}.pulse.catchup{background:var(--developing);box-shadow:0 0 12px var(--developing)}.pulse.error{background:var(--breaking);box-shadow:0 0 12px var(--breaking)}
.header-right{display:flex;align-items:center;gap:14px}
.theme-toggle{appearance:none;width:34px;height:34px;flex:0 0 auto;border:1px solid var(--line);background:var(--pill);color:var(--text);border-radius:8px;font-size:15px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:border-color .12s,background-color .12s}.theme-toggle:hover{border-color:var(--amber)}.theme-toggle:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.strip{display:flex;align-items:stretch;gap:12px;margin-top:14px}
/* Operator-only telemetry: hidden unless ?ops is set (see head script). */
:root:not([data-ops="1"]) .pipeline-health,:root:not([data-ops="1"]) .connection,:root:not([data-ops="1"]) .cost{display:none}
.cost{display:flex;flex-wrap:wrap;align-items:center;gap:6px 16px;margin-top:10px;padding:8px 14px;border:1px solid var(--line);background:var(--pill);color:var(--muted);font-size:9px;letter-spacing:.1em;text-transform:uppercase}
.cost .lead{color:var(--amber)}.cost b{color:var(--text);font-weight:650;font-variant-numeric:tabular-nums}.cost .calls{color:var(--dim);text-transform:none;letter-spacing:0}
.pipeline-health{flex:0 0 auto;display:flex;flex-wrap:wrap;align-items:center;gap:6px 18px;padding:9px 14px;border:1px solid var(--line);background:var(--pill);color:var(--muted);font-size:9px;letter-spacing:.1em;text-transform:uppercase}.pipeline-health b{color:var(--text);font-weight:650}.pipeline-health .good b{color:var(--confirmed)}.pipeline-health .warn b{color:var(--developing)}.pipeline-health .bad b{color:var(--breaking)}
.metrics{flex:1 1 auto;display:flex;flex-wrap:wrap;justify-content:center;gap:10px;margin:0}
.metric{appearance:none;margin:0;padding:8px 14px;background:var(--pill);border:1px solid var(--line);border-radius:999px;font:inherit;font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);cursor:pointer;display:inline-flex;align-items:center;gap:8px;white-space:nowrap;transition:background-color .12s,border-color .12s,color .12s}
.metric b{font-size:13px;font-weight:700;color:var(--text);font-variant-numeric:tabular-nums;line-height:1}
.metric.breaking{--accent:var(--breaking)}.metric.unconfirmed{--accent:var(--unconfirmed)}.metric.developing{--accent:var(--developing)}.metric.confirmed{--accent:var(--confirmed)}.metric.contradicted{--accent:var(--contradicted)}.metric.all{--accent:var(--amber)}
.metric:hover{border-color:color-mix(in srgb,var(--accent,var(--amber)) 55%,var(--line));color:var(--text)}
.metric:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.metric.active{background:color-mix(in srgb,var(--accent) 16%,var(--surface));border-color:var(--accent);color:var(--accent)}.metric.active b{color:var(--accent)}
.tape{flex:1 1 auto;min-width:0;min-height:46px;overflow:hidden;display:flex;align-items:center;border:1px solid var(--line);background:var(--panel);white-space:nowrap;-webkit-mask-image:linear-gradient(90deg,transparent,#000 6%,#000 94%,transparent);mask-image:linear-gradient(90deg,transparent,#000 6%,#000 94%,transparent)}.tape-track{display:inline-flex;min-width:max-content;animation:crawl var(--duration,50s) linear infinite}.tape:hover .tape-track{animation-play-state:paused}.tape-group{display:inline-flex}.tick{font-size:12.5px;color:var(--muted)}.tick::before{content:"◆";color:var(--line);margin:0 20px}.tick.breaking{color:var(--breaking)}.tick.confirmed{color:var(--confirmed)}.tick.unconfirmed{color:var(--unconfirmed)}
a.tick{text-decoration:none;cursor:pointer}a.tick:hover{text-decoration:underline;text-underline-offset:3px}a.tick:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
@keyframes crawl{to{transform:translateX(-50%)}}
main{width:100%;margin:0;padding:38px clamp(18px,4vw,60px) 76px}.board-head{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:18px}.board-head h2{margin:0;font-size:15px;letter-spacing:.15em;text-transform:uppercase}.board-head p{margin:7px 0 0;color:var(--muted);font-size:11px}.updated{font-size:10px;color:var(--dim);font-variant-numeric:tabular-nums}
/* Masonry rows: 1px keeps span quantization sub-pixel. Row spacing is the child
   margin, not row-gap — a row-gap is charged per row and would coarsen the grid. */
.feed{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),1fr));grid-auto-rows:1px;align-items:start;column-gap:16px;row-gap:0}.feed>*{margin-bottom:16px}.card{position:relative;min-width:0;padding:22px;border:1px solid var(--line);border-top:3px solid var(--state);background:linear-gradient(145deg,var(--card-sheen),transparent 45%),var(--surface)}.card.breaking{--state:var(--breaking);background:linear-gradient(145deg,color-mix(in srgb,var(--breaking) 12%,transparent),transparent 48%),var(--surface);box-shadow:0 0 0 1px color-mix(in srgb,var(--breaking) 12%,transparent),0 15px 50px var(--shadow)}.card.confirmed{--state:var(--confirmed)}.card.developing{--state:var(--developing)}.card.contradicted{--state:var(--contradicted)}.card.unconfirmed{--state:var(--unconfirmed)}
.card{scroll-margin-top:calc(var(--header-h,164px) + 14px)}.card:target{box-shadow:0 0 0 2px var(--amber),0 14px 44px var(--shadow);animation:flash 1.1s ease-out}@keyframes flash{0%{box-shadow:0 0 0 3px var(--amber),0 0 30px var(--amber)}100%{box-shadow:0 0 0 2px var(--amber),0 14px 44px var(--shadow)}}
.badge{display:inline-flex;align-items:center;min-height:32px;padding:7px 10px;border:1px solid color-mix(in srgb,var(--state) 55%,transparent);background:color-mix(in srgb,var(--state) 11%,transparent);color:var(--state);font-size:10px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}.badge::before{content:"";width:7px;height:7px;border-radius:50%;margin-right:8px;background:var(--state);box-shadow:0 0 9px var(--state)}
.card-meta{display:flex;justify-content:space-between;gap:16px;align-items:start;margin-bottom:16px}.importance{color:var(--amber);font-size:9px;letter-spacing:2px}.importance i{color:color-mix(in srgb,var(--text) 22%,transparent);font-style:normal}.headline{margin:0 0 10px;font:650 clamp(17px,1.7vw,21px)/1.38 "IBM Plex Mono","SFMono-Regular",Consolas,monospace;overflow-wrap:anywhere}.summary{margin:0;color:color-mix(in srgb,var(--text) 78%,transparent);font-size:13px;line-height:1.6}.origin{margin-top:14px;color:var(--muted);font-size:10px;letter-spacing:.04em}.origin strong{color:var(--text)}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));grid-auto-rows:1px;align-items:start;column-gap:8px;row-gap:0;margin-top:18px}.fact{margin-bottom:8px;padding:10px;border:1px solid var(--inset-line);background:var(--lift)}.fact b{display:block;color:var(--text);font-size:14px;margin-bottom:3px}.fact span{color:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:.08em}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:14px}.chip{padding:5px 8px;border:1px solid var(--inset-line);background:var(--lift);font-size:10px}.chip.up{color:var(--confirmed);border-color:color-mix(in srgb,var(--confirmed) 45%,var(--line))}.chip.down{color:var(--breaking);border-color:color-mix(in srgb,var(--breaking) 45%,var(--line))}
.sources{margin:18px 0 0;padding:15px 0 0;border-top:1px solid var(--line);list-style:none}.sources li+li{margin-top:9px}.sources a{color:var(--cyan);font-size:11px;line-height:1.45;text-decoration:underline;text-decoration-color:rgba(117,203,208,.35);text-underline-offset:3px}.sources a:focus-visible{outline:2px solid var(--amber);outline-offset:3px}.publisher{color:var(--muted);font-size:10px}.lag{margin-top:12px;color:var(--confirmed);font-size:10px}.empty{grid-column:1/-1;padding:90px 24px;border:1px dashed var(--line);color:var(--muted);text-align:center;line-height:1.8}.error-message{color:var(--breaking)}
footer{padding:20px;border-top:1px solid var(--line);color:var(--dim);text-align:center;font-size:9px;letter-spacing:.1em}
@media(max-width:1180px){.metrics{flex:1 1 100%;order:3;margin-top:2px}}
@media(max-width:760px){.strip{flex-direction:column}.pipeline-health{justify-content:center}.tape{order:2;min-height:46px}}
@media(max-width:640px){.topnav{align-items:flex-start}.board-head{align-items:flex-start;flex-direction:column;gap:10px}.brand{display:block}.brand span{display:block;margin-top:6px}.feed{grid-template-columns:1fr}.metrics{gap:8px}}
@media(prefers-reduced-motion:reduce){.tape-track{animation:none}}
</style>
</head>
<body>
<a class="skip" href="#feed">Skip to event board</a>
<header>
  <div class="topnav">
    <div class="brand"><h1>FinTick_</h1><span>ahead of the wire</span></div>
    <div class="metrics" id="metrics" role="group" aria-label="Filter events by validation status (select one or more)"></div>
    <div class="header-right"><button class="theme-toggle" id="theme-toggle" type="button" aria-label="Switch between light and dark theme">☾</button><div class="connection" role="status"><i class="pulse" id="pulse" aria-hidden="true"></i><span id="connection">CONNECTING</span></div></div>
  </div>
  <div class="strip">
    <div class="pipeline-health" id="pipeline-health" aria-label="Pipeline accounting">Awaiting pipeline health…</div>
    <div class="tape" aria-label="Latest event ticker"><div class="tape-track" id="tape"></div></div>
  </div>
  <div class="cost" id="cost" aria-label="Inference cost tracker"></div>
</header>
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
function safeStatus(value){return['breaking','confirmed','contradicted','developing','unconfirmed'].includes(value)?value:'developing'}
function badgeText(item){const n=Array.isArray(item.validations)?item.validations.length:0;switch(safeStatus(item.status)){case'breaking':return'BREAKING — no corroboration yet';case'unconfirmed':return'UNCONFIRMED — wire still silent';case'confirmed':return'CONFIRMED — '+n+' source'+(n===1?'':'s');case'contradicted':return'CONTRADICTED';default:return'DEVELOPING'}}
function lagText(seconds){if(!Number.isFinite(seconds))return'';const abs=Math.abs(seconds),value=abs<3600?Math.round(abs/60)+' min':(abs/3600).toFixed(1)+' hr';return seconds>=0?'news +'+value+' after the stream':'news '+value+' before the stream'}
function renderPipeline(value){const p=value&&typeof value==='object'?value:{},node=$('pipeline-health'),backlog=Number(p.backlog)||0,errors=Number(p.terminal_errors)||0,accounted=Number(p.accounted)||0,posts=Number(p.posts)||0;node.replaceChildren();let state='CAUGHT UP';if(errors>0){state='TERMINAL ERRORS'}else if(backlog>0){state='CATCHING UP'}const coverage=element('span','');coverage.append(document.createTextNode('accounted '),element('b','',accounted+' / '+posts));node.append(coverage);const queue=element('span',backlog?'warn':'good');queue.append(document.createTextNode('backlog '),element('b','',String(backlog)));node.append(queue);if(backlog&&p.oldest_pending_at){const oldest=element('span','');oldest.append(document.createTextNode('oldest '),element('b','',relativeTime(p.oldest_pending_at)));node.append(oldest)}if(errors){const terminal=element('span','bad');terminal.append(document.createTextNode('errors '),element('b','',String(errors)));node.append(terminal)}$('connection').textContent=state;$('pulse').classList.toggle('catchup',backlog>0&&!errors);$('pulse').classList.toggle('error',errors>0);renderCost(p.cost)}
function renderCost(cost){const node=$('cost');if(!node)return;node.replaceChildren();const c=cost&&typeof cost==='object'?cost:{},labels={hour:'1H',day:'24H',week:'7D',month:'30D'};node.append(element('span','lead','inference cost'));for(const key of ['hour','day','week','month']){const w=c[key]||{},usd=Number(w.usd)||0,calls=Number(w.calls)||0,span=element('span','');span.append(document.createTextNode(labels[key]+' '),element('b','','$'+usd.toFixed(usd<1?4:2)));if(calls)span.append(element('span','calls',' ('+calls+' call'+(calls===1?'':'s')+')'));node.append(span)}}
const FILTERS=[['all','all'],['breaking','breaking'],['unconfirmed','unconfirmed'],['developing','developing'],['confirmed','confirmed'],['contradicted','contradicted']];
const activeFilters=new Set();
// Contradicted events are off the board unless their pill is selected. The pill still
// carries the real count, so hidden never means gone.
const DEFAULT_HIDDEN=new Set(['contradicted']);
function boardPool(items){return items.filter(x=>!DEFAULT_HIDDEN.has(x.status)||activeFilters.has(x.status))}
function statusCount(items,status){return status==='all'?boardPool(items).length:items.filter(x=>x.status===status).length}
function renderMetrics(items){const metrics=$('metrics');metrics.replaceChildren();for(const [status,label] of FILTERS){const on=status==='all'?activeFilters.size===0:activeFilters.has(status),box=element('button','metric '+status+(on?' active':''));box.type='button';box.setAttribute('aria-pressed',String(on));box.append(element('span','',label),element('b','',String(statusCount(items,status))));box.addEventListener('click',()=>{if(status==='all')activeFilters.clear();else if(activeFilters.has(status))activeFilters.delete(status);else activeFilters.add(status);applyFilter()});metrics.append(box)}}
function tick(item,hidden){const cls='tick '+safeStatus(item.status),text=String(item.headline||'');if(item.id===undefined||item.id===null)return element('span',cls,text);const link=element('a',cls,text);link.href='#evt-'+item.id;link.title='Jump to this event';if(hidden)link.tabIndex=-1;return link}
function renderTape(items){const tape=$('tape');tape.replaceChildren();if(!items.length){tape.append(element('span','tick','No events yet'));return}function group(hidden){const node=element('div','tape-group');if(hidden)node.setAttribute('aria-hidden','true');for(const item of items.slice(0,24))node.append(tick(item,hidden));return node}tape.append(group(false),group(true));tape.style.setProperty('--duration',Math.max(38,items.length*8)+'s')}
function renderCard(item){const status=safeStatus(item.status),card=element('article','card '+status);if(item.id!==undefined&&item.id!==null)card.id='evt-'+item.id;const meta=element('div','card-meta');meta.append(element('span','badge',badgeText(item)));if(Number.isInteger(item.importance)){const rank=element('span','importance');rank.setAttribute('aria-label','Importance '+item.importance+' of 5');rank.append(document.createTextNode('◆'.repeat(item.importance)),element('i','', '◆'.repeat(5-item.importance)));meta.append(rank)}card.append(meta,element('h3','headline',String(item.headline||'Untitled event')));if(item.summary)card.append(element('p','summary',String(item.summary)));const origin=element('p','origin');origin.append(document.createTextNode('via the stream · seen '),element('strong','',String(item.stream_seen||0)+'×'),document.createTextNode(' · '+relativeTime(item.first_seen_at)));card.append(origin);
const facts=Array.isArray(item.facts)?item.facts:[];if(facts.length){const grid=element('div','facts');for(const fact of facts){if(!fact||fact.value===undefined)continue;const node=element('div','fact');node.append(element('b','',String(fact.value)+(fact.unit?' '+fact.unit:'')),element('span','',String(fact.label||'fact')));grid.append(node)}if(grid.childNodes.length)card.append(grid)}
const instruments=Array.isArray(item.instruments)?item.instruments:[];if(instruments.length){const chips=element('div','chips');for(const instrument of instruments){const direction=['up','down','flat'].includes(instrument.direction)?instrument.direction:'flat';const marker=direction==='up'?'▲ ':direction==='down'?'▼ ':'— ';const chip=element('span','chip '+direction,marker+String(instrument.symbol||instrument.name||''));chip.title=String(instrument.name||instrument.symbol||'Instrument');chips.append(chip)}card.append(chips)}
const links=Array.isArray(item.validations)?item.validations:[];if(links.length){const list=element('ul','sources');for(const story of links){if(!story||!story.url)continue;const li=element('li'),link=element('a','',String(story.title||story.url));link.href=String(story.url);link.target='_blank';link.rel='noopener noreferrer';li.append(link,document.createTextNode(' '),element('span','publisher','— '+String(story.publisher||'external news')));list.append(li)}if(list.childNodes.length)card.append(list)}const lag=lagText(item.lead_seconds);if(lag)card.append(element('p','lag',lag));return card}
let lastItems=[],lastGeneratedAt=null;
function visibleItems(){const pool=boardPool(lastItems);return activeFilters.size===0?pool:pool.filter(x=>activeFilters.has(x.status))}
function renderFeed(){const items=visibleItems(),sel=[...activeFilters];setFeed(items.length?items.map(item=>renderCard(item)):[element('div','empty',lastItems.length?('No '+(sel.length?sel.join(' / ')+' ':'')+'events to show.'):'No aggregated events yet. Ingest the stream, then run FinTick aggregate.')]);const generated=new Date(lastGeneratedAt),total=boardPool(lastItems).length,shown=items.length,count=sel.length===0?total+' events':shown+' of '+total+' · '+sel.join(' + '),stamp=Number.isNaN(generated.valueOf())?'':' · updated '+generated.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});$('updated').textContent=count+stamp}
// Every #feed mutation goes through setFeed: the CSS declares 1px rows, so a node
// added without a matching row span collapses to 1px and paints over the footer.
function setFeed(nodes){const feed=$('feed');feed.replaceChildren();for(const node of nodes)feed.append(node);layoutMasonry()}
// Pack one 1px-row grid: measure every child, give it a row span matching its own
// height. Used for both the board and the fact grids inside each card.
function packGrid(grid){const kids=[...grid.children];if(!kids.length)return;const styles=getComputedStyle(grid),row=parseFloat(styles.gridAutoRows)||1,gap=parseFloat(styles.rowGap)||0,spacer=parseFloat(getComputedStyle(kids[0]).marginBottom)||0;for(const k of kids)k.style.gridRowEnd='';const heights=kids.map(k=>k.getBoundingClientRect().height);kids.forEach((k,i)=>{const h=heights[i];if(h)k.style.gridRowEnd='span '+Math.max(1,Math.ceil((h+spacer+gap)/(row+gap)))})}
function unpackGrid(grid){for(const k of grid.children)k.style.gridRowEnd='';grid.style.gridAutoRows='auto'}
function layoutMasonry(){const feed=$('feed');if(!feed)return;const cards=[...feed.children];if(!cards.length)return;const factGrids=[...feed.querySelectorAll('.facts')];try{
// Facts pack FIRST: reclaiming their dead space shortens the card, so measuring
// cards before this would bake in the taller pre-pack height and leave a gap.
for(const grid of factGrids)packGrid(grid);packGrid(feed);feed.style.gridAutoRows=''}catch(error){/* Half-spanned cards overlap and are unreadable; a plain grid is not. */
for(const grid of factGrids)unpackGrid(grid);unpackGrid(feed)}}
function syncHeaderOffset(){const header=document.querySelector('header');if(!header)return;const h=Math.round(header.getBoundingClientRect().height);if(h)document.documentElement.style.setProperty('--header-h',h+'px')}
addEventListener('resize',syncHeaderOffset);syncHeaderOffset();
function applyFilter(){renderMetrics(lastItems);renderFeed()}
function render(data){lastItems=Array.isArray(data.items)?data.items:[];lastGeneratedAt=data.generated_at;renderPipeline(data.pipeline);renderTape(boardPool(lastItems));applyFilter();syncHeaderOffset()}
function currentTheme(){const set=document.documentElement.dataset.theme;if(set==='light'||set==='dark')return set;return matchMedia('(prefers-color-scheme: light)').matches?'light':'dark'}
function syncThemeIcon(){$('theme-toggle').textContent=currentTheme()==='light'?'☀':'☾'}
function toggleTheme(){const next=currentTheme()==='light'?'dark':'light';document.documentElement.dataset.theme=next;try{localStorage.setItem('fintick-theme',next)}catch(e){}syncThemeIcon()}
$('theme-toggle').addEventListener('click',toggleTheme);syncThemeIcon();
matchMedia('(prefers-color-scheme: light)').addEventListener('change',()=>{if(!document.documentElement.dataset.theme)syncThemeIcon()});
let refreshInFlight=false;async function refresh(){if(refreshInFlight)return;refreshInFlight=true;try{const response=await fetch('/api/feed',{cache:'no-store'});if(!response.ok)throw new Error('HTTP '+response.status);render(await response.json())}catch(error){$('connection').textContent='RECONNECTING';$('pulse').classList.add('error');if(!$('feed').querySelector('.card'))setFeed([element('div','empty error-message','Event feed unavailable. FinTick will retry automatically.')])}finally{refreshInFlight=false}}
let masonryTimer=null;function scheduleMasonry(){if(masonryTimer)clearTimeout(masonryTimer);masonryTimer=setTimeout(layoutMasonry,120)}
addEventListener('resize',scheduleMasonry);if(document.fonts&&document.fonts.ready)document.fonts.ready.then(layoutMasonry);
layoutMasonry(); refresh(); setInterval(refresh, 20000);
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
    # Social scrapers and link validators HEAD an og:image before fetching it, and the
    # base handler answers 501 for any verb it has no do_* for — which reads as a broken
    # image. HEAD routes through do_GET and drops the body.
    _head_only = False

    def do_HEAD(self) -> None:  # noqa: N802
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        # HEAD keeps every header, including Content-Length, but sends no body.
        if not self._head_only:
            self.wfile.write(body)

    def _send_asset(self, name: str, content_type: str) -> None:
        try:
            body = (ASSET_DIR / name).read_bytes()
        except OSError as error:
            self.log_error("asset %s unavailable: %s", name, error)
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        self._send(HTTPStatus.OK, body, content_type, cache_control="public, max-age=86400")

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path == "/robots.txt":
            body = ROBOTS_TXT.format(origin=SITE_ORIGIN).encode()
            self._send(HTTPStatus.OK, body, "text/plain; charset=utf-8",
                       cache_control="public, max-age=3600")
            return
        if parts.path == "/sitemap.xml":
            body = SITEMAP_XML.format(origin=SITE_ORIGIN).encode()
            self._send(HTTPStatus.OK, body, "application/xml; charset=utf-8",
                       cache_control="public, max-age=3600")
            return
        if parts.path == "/llms.txt":
            self._send(HTTPStatus.OK, LLMS_TXT.encode(), "text/plain; charset=utf-8",
                       cache_control="public, max-age=3600")
            return
        asset = ASSET_ROUTES.get(parts.path)
        if asset is not None:
            self._send_asset(*asset)
            return
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
