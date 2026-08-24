# FinTick — Product Requirements Document

> **Designed by Claude Opus 4.8 · Built by Qwen 3.8 27B via Hermes.**
> This document is the specification. You (the Hermes agent, running on local Qwen 3.8 27B)
> are the builder. Read `AGENTS.md` for how to work; read `reference/` for the facts you need.

---

## 1. Vision

A 24/7, self-updating financial **news ticker** — an old-school tape — fed entirely by one
exceptional source: the `fintwitter.bsky.social` account on Bluesky. That account is a
Bloomberg-terminal-grade firehose: commodity settlements, geopolitics, macro, central-bank
moves, and equity headlines, posted fast and continuously. FinTick captures every message,
removes the noise, understands each headline with a **local** LLM, enriches it with global
instrument symbols and related context, and presents it as a live, glanceable dashboard.

**Why it's special:** most "news dashboards" scrape dozens of noisy sources. FinTick drinks from
one curated, high-signal firehose and adds intelligence on top: dedup, structured tagging,
symbol extraction, and researched context — all offline, on hardware you own.

## 2. The source

- Account: `fintwitter.bsky.social` — DID `did:plc:43fdk46qa5gsokzygzildsaq`
- Read via the **public, unauthenticated** Bluesky AppView API (see `reference/bluesky_api.md`).
- Character: high-frequency, bursty, **heavily duplicated** (the same headline is often posted
  seconds apart in ALLCAPS and Title Case). A real captured sample is in
  `reference/feed_sample.json` (60 posts) — build and test against it offline first.

## 3. Users & usage

A single operator (Michael) glances at a wall/desktop dashboard throughout the day to stay on
top of markets. Optimize for **glanceability** and **freshness**, not interactivity.

## 4. Functional requirements

**F1 — Ingest.** Poll the feed on a schedule (default every 15 min; make it configurable).
Walk pagination back to the last-seen post so no message is ever missed between polls. Store
every post durably. The post `uri` is the natural primary key (idempotent re-fetch).

**F2 — Dedup (mandatory).** Collapse the ALLCAPS/Title-Case reposts. Normalize text, hash it,
and within a time window treat repeats as duplicates of the first ("canonical") occurrence.
Keep the duplicates in storage (for auditing) but the ticker shows canonical items only.
See `reference/dedup_insight.md`.

**F3 — Enrich (local LLM).** For each canonical post, use the **local** Qwen model to produce:
- a one-sentence plain-English summary (expand jargon, keep the numbers),
- a category (one of: commodities, equities, macro, central-bank, geopolitics, fx, rates, crypto, other),
- an importance score 1–5 (5 = major market-moving),
- a sentiment (bullish / bearish / neutral) for the primary asset,
- **instruments**: a list of *global* tradable symbols mentioned — equities WITH exchange
  suffix (`7203.T`, `BP.L`, `AAPL`), commodity futures roots (`CL` WTI, `BZ` Brent, `NG`
  NatGas, `GC` gold), FX pairs (`USDJPY`), indices (`SPX`, `N225`), crypto (`BTC`) — each with
  a human name, type, venue if known, and direction (up/down/flat) from the headline. Do NOT
  invent tickers; omit when unsure.
- entities (people, companies, institutions, countries) and regions.

  Enrichment must be **resilient**: a bad/unparseable model response for one item must never
  crash the pipeline or block others; mark it errored and move on (retry later).

**F4 — Research related stories.** For higher-importance items (e.g. importance ≥ 3), use the
web/browser tools to find 1–2 genuinely related news stories and attach `{title, url, source}`.
This is what turns a headline tape into an intelligence feed. Keep it bounded (don't research
every trivial settle print) and cache so you don't re-research the same item.

**F5 — Dashboard (the tape).** A single, self-contained, auto-refreshing web page:
- A top **scrolling ticker tape** of the latest canonical headlines with symbols and up/down
  coloring — the "old-school ticker" feel.
- Below it, a live **feed of cards**: summary, category (color-coded), importance, sentiment,
  extracted symbols as chips, and any researched related links.
- Dark "terminal" aesthetic (think amber/green on near-black). Monospace or condensed type.
- Auto-refreshes (poll a small JSON endpoint or reload) every ~15–30 s. No manual action needed.
- Must degrade gracefully: unenriched items still show as raw headlines immediately; enrichment
  fills in as it completes.

**F6 — Run as a process (24/7).** The system runs continuously, unattended, and survives reboots.
Because sudo is not available to you, you must **write a supervisor install script**
(`setup-fintick-supervisor.sh`, author-script style) for Michael to run — see `AGENTS.md`. You
self-test by running the processes under your own user (`setsid`/`nohup`) and confirming they
stay up and the dashboard renders.

## 5. Non-functional requirements

- **All-local inference.** Enrichment/research reasoning runs on the local Qwen model only
  (`http://localhost:11434`). No paid/cloud AI APIs. (There are no cloud keys configured — good.)
- **No paid data APIs.** Only the free public Bluesky API and free web research.
- **Dependency-light.** Prefer the Python standard library (see `AGENTS.md` for why).
- **Resilient & idempotent.** Safe to restart at any time; never double-counts; never loses posts.
- **Observable.** Log what it does each cycle (counts: fetched / new / deduped / enriched / errored).

## 6. Suggested architecture (you may improve on it)

Four concerns, cleanly separated — you decide the exact process/file layout:
1. **ingest** — poll + dedup + store raw.
2. **enrich** — local-LLM structured tagging of canonical posts.
3. **research** — web lookups for high-importance items (can be part of enrich or its own worker).
4. **dashboard** — render/serve the tape from storage.

Storage: a single SQLite database at `./data/fintick.db` in the repo workdir.

## 7. Acceptance criteria (all must pass = DONE)

1. Running ingest against `reference/feed_sample.json` (offline mode) populates the DB and
   **correctly collapses the known duplicates** into canonical + duplicate rows.
2. A live ingest run fetches real posts and is **idempotent** (running twice adds no dupes).
3. Enrich produces valid structured records for canonical posts using the **local** model, with
   at least summary + category + importance + instruments populated, and never crashes on a bad
   model response.
4. For a sample of high-importance items, research attaches ≥1 related link (best-effort; if the
   web tools are unavailable at build time, the code path exists and is unit-tested with a stub).
5. The dashboard renders: a scrolling ticker + enriched cards with color-coded categories and
   symbol chips, and auto-refreshes without manual reload.
6. All components run as long-lived processes under the user; `setup-fintick-supervisor.sh`
   exists and is correct for Michael to install them under supervisor.
7. The repo is a clean, push-ready git repository: `README.md` (carrying the provenance line),
   `LICENSE` (MIT), `.gitignore` (excludes `data/`, DBs, caches), and milestone commits. The DB
   and any secrets are NOT committed.

## 8. Out of scope / stretch

- Out of scope: authentication, multi-user, alerting/notifications, mobile app, trading actions.
- Stretch (only after 1–7 pass): sparkline price context per symbol, per-category filter toggles
  on the dashboard, a "most-mentioned symbols in the last hour" leaderboard, TLS/public hosting.

## 9. Provenance & repo identity

- Project name: **FinTick**. Repo name: `fintick`.
- `README.md` MUST include, prominently:
  > **Designed by Claude Opus 4.8 · Built by Qwen 3.8 27B via Hermes.**
- Tone of README: proud, clear, a little playful about the human/AI/local-AI collaboration.
