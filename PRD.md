# FinTick — Product Requirements Document (v2: stream-signal + validation)

> **Designed by Claude Opus 4.8 · Built by Qwen 3.8 27B via Hermes — local model ONLY.**
> This document is the specification. You (the Hermes agent on local Qwen 3.8 27B) are the builder.
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

**F4 — Hunt for external validation (THE CORE STAGE).** For each event, search **independent news**
(free RSS / news-search JSON via `curl` from the `terminal` tool — NO browser, see AGENTS.md) for
stories corroborating the facts. Attach `validating_sources[]` `{url, title, publisher, stance,
published_at}`. Set `status`:
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

**F6 — Run as a process (24/7, KEEP from v1).** Runs continuously, survives reboots. You have **no
sudo**: (re)write `setup-fintick-supervisor.sh` (author-script style, `user=michael`) for Michael to
run — one program per long-running worker (ingest, aggregate, validate, dashboard). **Use a safe
port for the dashboard — NOT 8080** (reserved/unsafe on this host); pick e.g. 8137 and make it
configurable. Self-test by launching the workers yourself with `setsid`/`nohup`.

## 5. Non-functional requirements

- **All-local inference.** Aggregation + fact extraction run on local Qwen only
  (`http://localhost:11434`, `qwen3.8:27b`). **No cloud AI** — the fallback is disabled at the
  Hermes level; if the local model errors, mark the item errored and move on, never defect.
- **No paid data APIs.** Free public Bluesky stream + free news/RSS for validation only.
- **Dependency-light**, stdlib-first (Python 3.14 — see AGENTS.md). Resilient, idempotent, observable
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
6. **Run-as-process:** ingest/aggregate/validate/dashboard run as long-lived workers under the user;
   `setup-fintick-supervisor.sh` exists, correct, and uses a **non-8080** dashboard port.
7. **Clean repo:** builds on `v1-baseline`; `README.md` updated for v2 (carrying the provenance
   line), `.gitignore` still excludes `data/`/DBs/caches; milestone commits; no DB/secrets committed.
   Do **not** `git push`.

## 8. Out of scope / stretch

- Out of scope: auth, multi-user, notifications, trading actions.
- Stretch (only after 1–7): a "breaking, still-unconfirmed" leaderboard; lead-time stats over time
  (how far ahead is the stream, by category?); per-instrument sparkline context.

## 9. Provenance & identity

- Project **FinTick**, repo `fintick`. `README.md` MUST prominently carry:
  > **Designed by Claude Opus 4.8 · Built by Qwen 3.8 27B via Hermes.**
- Keep the tone proud, clear, a little playful about the human / AI / local-AI collaboration — and
  now about the **edge**: a local model catching breaking markets before the wire.
