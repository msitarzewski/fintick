# FinTick — Product Requirements Document (v2: stream-signal + validation)

> **Designed by Claude Opus · Built with Qwen 3.8 and GPT 5.6 Sol via Hermes.**
> This document is the product specification; provider and operations rules live in `AGENTS.md`.
> Read `AGENTS.md` for how to work; read `reference/` for the facts you need.
>
> **This is v2.** v1 (a per-post ticker with hash-dedup + per-post enrich) is tagged `v1-baseline`
> and works. v2 keeps v1's **ingest + storage** unchanged and **replaces the middle** of the
> pipeline. Refactor the existing code; do not start from scratch.

---

## 1. Vision — catch the signal before the wire

FinTick watches **one** exceptional stream and surfaces what it's saying **before the news
confirms it**. The old framing (a de-duplicated headline ticker) missed the point: the value isn't
a tidy tape, it's the **edge** — the stream reports market-moving events *fast*, often ahead of any
published story. FinTick's job is to (a) collapse the stream's repeated posts of the same event into
**one event**, (b) extract the hard facts, and (c) **go hunt for independent news that validates
it** — and when it can't find any, say so loudly, because **an unvalidated breaking event is the
most valuable thing on the board.**

> The product is the **delta between "the stream said it" and "the news confirms it."**

## 2. The source — ONE stream (this framing matters)

- Account: `fintwitter.bsky.social` — DID `did:plc:43fdk46qa5gsokzygzildsaq`, read via the **public,
  unauthenticated** Bluesky AppView API (see `reference/bluesky_api.md`).
- It is **one stream** — a single fast source, **with no URLs**. It is *the signal*, **never a
  "source."** When it posts the same event four times in different wording/casing, that is **one
  event from one origin (the stream)** — not four sources. Real sample: `reference/feed_sample.json`
  (60 posts). The canonical v2 fixture is `reference/nvda_repost_cluster.json` (the four NVDA posts).
- "Sources" in FinTick means **external validating stories** the app finds — real news, with URLs,
  independent of the stream. Count those; never count stream posts as sources.

## 3. Users & usage

A single operator (Michael) glances at a wall/desktop dashboard to see, at a glance: **what did the
stream just catch, and has the news caught up yet?** Optimize for glanceability and for making
**breaking-but-unvalidated** events obvious.

## 4. Functional requirements

**F1 — Ingest (KEEP from v1).** Poll the stream on a schedule (default 15 min, configurable), walk
pagination to the last-seen post so nothing is missed, store every post durably (post `uri` = PK,
idempotent). Unchanged from v1 — reuse it.

**F2 — Aggregate into events (REPLACES v1 dedup+enrich).** Each run, take stream posts in a rolling
**6-hour window** (hard cap **≤ 200 posts** per pass; if more, process in order — but keep a single
pass over the window so repeats of one event are seen together). Make **one structured local-Qwen
call** that returns the **distinct events**. For each event:
- `canonical_headline`, one-sentence `summary`,
- `instruments[]` — global symbols, names unified (`$NVDA` and `NVIDIA` → one instrument), each with
  human name, type, direction (up/down/flat),
- `stream_post_uris[]` — the posts from the stream that make up this event (the SIGNAL),
- `importance` 1–5.
The four NVDA posts MUST collapse into **one** event. **Aggregation replaces hash-dedup** — do not
rely on normalized-hash matching; the model merging semantically is the mechanism. Must be
resilient: a bad/unparseable model response never crashes the pipeline or blocks other events.

**F3 — Extract facts.** For each event, extract structured claims from the summary, e.g.
`$NVDA · 7th consecutive down day · −3.2% · longest streak since 2022`. May be produced as part of
the F2 call or a second pass. Store as structured data on the event.

**F4 — Hunt for external validation (THE CORE STAGE).** For each event, search **independent news**.
When configured, use the authenticated common.vision Partner API index, which includes Google News
feeds; retain direct public RSS as the no-token compatibility path. Social posts are not independent
corroboration. Attach `validating_sources[]` `{url, title, publisher, stance,
published_at, feed_name, feed_url, feed_type}`. For common.vision, `publisher` comes from
`Article.metadata.source`; `Article.feed` is ingestion provenance and must not be counted as a second
independent publisher. Set `status`:
- **`breaking`** — a real event with **0 external validating sources found** → *the stream is ahead
  of the news.* This is a first-class, highlighted result, NOT a failure.
- **`confirmed`** — ≥ 1 corroborating external source. Record how far behind the stream the news was
  (lead time).
- **`contradicted`** — an external source disputes it. **`developing`** — weak/partial.
Validation must **re-run** on later fires so a `breaking` event can flip to `confirmed` when the
wire catches up. Bounded + cached (don't re-hunt the same event every fire).

**F5 — Dashboard (the board).** A single self-contained, auto-refreshing page, dark terminal
aesthetic:
- Event cards (NOT raw-post cards): headline, facts, instruments as chips with up/down color.
- **Origin line:** "via the stream · seen N×" — **never "N sources."**
- **Validation badge is the focal point:** 🔴 `BREAKING — no corroboration yet` (highlighted,
  sorted up), 🟢 `CONFIRMED — N sources` (+ links, + lead time e.g. "news +37 min after the stream"),
  ⚠️ `CONTRADICTED`, 🟡 `DEVELOPING`.
- Auto-refresh ~15–30 s. Degrades gracefully: a fresh event shows immediately as `breaking` and
  gains a badge as validation completes.

**F6 — Run as a process (24/7, KEEP from v1).** Runs continuously and survives reboots using the
host's established rootless user-systemd convention. `setup-fintick-services.sh` installs one unit
per long-running worker (ingest, aggregate, validate, dashboard). **Use a safe
port for the dashboard — NOT 8080** (reserved/unsafe on this host); pick e.g. 8137 and make it
configurable. Self-test with installer dry-run, user-systemd status, journal output, and loopback HTTP health.

## 5. Non-functional requirements

- **Provider-injected inference.** Aggregation defaults to GPT-5.6 Luna through Hermes-managed
  `openai-codex` OAuth; FinTick never reads or copies credentials. The Ollama-compatible local route
  remains injectable and offline-testable. Provider failures create durable retry/error decisions.
- **No paid data dependency.** Public Bluesky plus the common.vision Partner API; direct free RSS
  remains available when no partner token is configured.
- **Dependency-light**, stdlib-first (Python 3.11+ — see AGENTS.md). Resilient, idempotent, observable
  (log per cycle: fetched / new / events / facts / validated / breaking / errored).

## 6. Architecture (refactor v1, don't rewrite)

Keep `ingest` + `storage`. Replace `dedup`/`enrich`/`research` with:
1. **aggregate** — window → distinct events (local-LLM) + signal mapping.
2. **facts** — structured claim extraction per event.
3. **validate** — external news hunt → sources + status (re-runnable).
4. **dashboard** — render the event board from storage.
New schema alongside the existing `posts` table: `events`, `event_signals` (event ↔ stream post),
`event_validations` (event ↔ external source w/ URL). SQLite at `./data/fintick.db`.

## 7. Acceptance criteria (all must pass = DONE)

1. **Merge:** aggregating `reference/nvda_repost_cluster.json` (offline) yields **exactly one
   event** whose `stream_post_uris` are all four posts, instruments unify `$NVDA`/`NVIDIA`.
2. **Breaking path:** with no live news reachable offline, that event's validation hunt returns **0
   external sources** and status **`breaking`** — proving the ahead-of-the-wire path.
3. **Facts:** the event carries structured facts (e.g. down-day count, % move) extracted by the
   **local** model; a bad model response never crashes the run.
4. **Validation (live/stub):** the F4 news-hunt code path exists and, given a stubbed corroborating
   source, sets status `confirmed` with the external link + a computed lead time. Re-running can flip
   `breaking` → `confirmed`.
5. **Dashboard:** renders **event** cards with the validation badge front-and-center, breaking events
   highlighted/sorted up, origin shown as "via the stream · seen N×" and **never** as "N sources".
   Auto-refreshes. **The v1 doubled-headline render bug is fixed.**
6. **Run-as-process:** ingest/aggregate/validate/dashboard run as long-lived rootless user services;
   `setup-fintick-services.sh` exists, is portable, and uses a **non-8080** dashboard port.
7. **Clean repo:** builds on `v1-baseline`; `README.md` updated for v2 (carrying the provenance
   line), `.gitignore` still excludes `data/`/DBs/caches; milestone commits; no DB/secrets committed.
   Do **not** `git push`.

## 8. Out of scope / stretch

- Out of scope: auth, multi-user, notifications, trading actions.
- Stretch (only after 1–7): a "breaking, still-unconfirmed" leaderboard; lead-time stats over time
  (how far ahead is the stream, by category?); per-instrument sparkline context.

## 9. Provenance & identity

- Project **FinTick**, repo `fintick`. `README.md` MUST prominently carry:
  > **Designed by Claude Opus · Built with Qwen 3.8 and GPT 5.6 Sol via Hermes.**
- Keep the tone proud, clear, a little playful about the human / AI / local-AI collaboration — and
  now about the **edge**: a local model catching breaking markets before the wire.
