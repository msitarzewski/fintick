DONE

# FinTick — Build Status (v2: stream-signal + validation)

FinTick v2 is complete: one stream becomes distinct events; each event carries structured facts and
stream-signal counts; independent RSS/news validation produces breaking/confirmed/contradicted/developing
status with lead time; and the responsive Edge Board makes uncorroborated events visually primary. The
canonical four-post NVDA fixture reaches one breaking event with four signals, facts, unified NVDA, and
zero external sources. All 69 tests pass; all four worker processes were launched and verified; desktop
and mobile interfaces were rendered and inspected; the dashboard runs at http://127.0.0.1:8137.

<!-- When ALL PRD.md §7 acceptance criteria pass, replace the line below with `DONE` + a
     one-paragraph summary, then run: hermes cron pause fintick-build -->
STATUS: DONE

**Current milestone:** Complete — M1 through M6 accepted 2026-08-25.

## The v2 pivot (read this first every fire)
v1 is tagged **`v1-baseline`** (a working per-post ticker) and its acceptance polish is stashed
(`git stash list` → "abandoned-m8-wip"). **Do NOT resume v1.** v2 **keeps v1's ingest + storage**
and **replaces the middle**: the old `dedup → enrich → research` becomes
**`aggregate → facts → validate`** (see PRD.md, rewritten). The core idea: the feed is **one
stream** (the signal, never "sources"); merge its repeats into one event; hunt **external** news to
validate; and an event with **0 external sources = `breaking`**, which is the point, not a failure.

Canonical test fixture: `reference/nvda_repost_cluster.json` — the four NVDA posts must become
**one** event, and offline must land as **`breaking`** (0 external sources).

> ⚠️ **Supersedes AGENTS.md:** the v1 hard-rule "Dedup is mandatory" is **retired** for v2 — do NOT
> hash-dedup. Aggregation (F2) is the merge mechanism. Ignore `reference/dedup_insight.md`.

## Milestones (tick as you finish; keep the tree runnable at every commit)
- [x] **M1 — Schema.** Add `events`, `event_signals` (event↔stream post), `event_validations`
      (event↔external source w/ URL) tables + migration. Keep the existing `posts` table + ingest
      untouched. Retire/neutralize the v1 `dedup`/`enrich`/`research` modules (don't delete blindly —
      leave the tree runnable). ✅ 2026-08-25: V2_SCHEMA additive (no ALTER/backfill); `upsert_event`
      idempotent on headline-hash key, never touches status; `record_validation` upserts (event,url);
      `set_event_status` owns status/lead_seconds; `load_events` joins stream_seen + validations.
      CLI help for retired v1 middle marked "RETAINED v1-baseline only". 59/59 tests green
      (47 legacy + 12 v2, `tests/test_storage_v2.py`). Verified: fresh DB has all 6 tables; v1-baseline
      DB gains exactly the 3 new tables on next open, idempotent; `ingest --fixture` round-trips 60 posts.
- [x] **M2 — Aggregate.** Rolling-6h-window batch → **one** local-Qwen call → distinct events +
      `stream_post_uris` mapping, schema-validated, resilient to bad JSON.
      **Acceptance: the NVDA fixture → 1 event, 4 signals, instruments unify `$NVDA`/`NVIDIA`.**
      ✅ 2026-08-25: `fintick aggregate` selects ≤200 posts from the latest six-hour window in
      chronological order, makes one forced-JSON Ollama call, validates each event independently,
      rejects unknown/reused URIs, normalizes symbols/directions, and idempotently persists through
      the M1 API. Canonical fixture passes offline with an injected model response: 4 posts → 1 event,
      4 signals, unified NVDA; malformed top-level output and malformed sibling events are isolated.
      67/67 tests green. Live `qwen3.8:27b` smoke is deferred: the runtime approval layer blocked the
      local endpoint check; do not treat model-quality behavior as live-verified yet.
- [x] **M3 — Facts.** Structured claim extraction per event (down-day count, % move, etc.), local model.
      ✅ 2026-08-25: Facts are extracted in the existing single F2 model call (no extra 27B pass) as
      validated `{label,value[,unit]}` objects, stored in `events.facts_json`, and exposed by
      `load_events`. Canonical NVDA acceptance carries down-day count and streak-year facts.
- [x] **M4 — Validate (core).** External news hunt via `curl` (RSS/news JSON, NO browser) →
      `validating_sources` + `status` (breaking / confirmed / contradicted / developing) + lead time.
      Re-runnable so `breaking` can flip to `confirmed`. **Fixture offline → `breaking`, 0 sources.**
      ✅ 2026-08-25: `fintick validate` performs a bounded, cached Google News RSS hunt, persists only
      safe HTTP(S) external sources, and derives status from source stance. Empty search is a successful
      `breaking` result; rerunning the same event with a stubbed corroborating story flips it to
      `confirmed` with the URL and a verified 600-second wire lag. Lookup failures are isolated and
      retained as event errors for retry. 69/69 tests green.
- [x] **M5 — Dashboard.** Event cards with the **validation badge front-and-center**; breaking events
      highlighted + sorted up; origin as "via the stream · seen N×" (NEVER "N sources"); lead time on
      confirmed. **Fix the v1 doubled-headline render bug** (title+text were concatenated).
      Dashboard port must be **non-8080** (e.g. 8137), configurable.
      ✅ 2026-08-25: `/api/feed` now returns event cards, status-prioritized with breaking first. The
      self-contained Edge Board renders focal validation badges, facts, instrument direction, the exact
      stream-origin language, safe external links, and news lag; refreshes every 20s; defaults to 8137.
      Desktop (1077px) and mobile (390px) renders were inspected live; a mobile heading/timestamp
      collision found during inspection was fixed and re-verified. 67/67 tests green.
- [x] **M6 — Acceptance.** Walk PRD §7 1–7; NVDA fixture end-to-end → one `breaking` event with
      facts; update README for v2; `setup-fintick-supervisor.sh` correct (non-8080 port).
      ✅ 2026-08-25: `tests/test_acceptance_v2.py` verifies the complete offline fixture path and
      operational docs. README/run-demo describe v2; Supervisor launches ingest/aggregate/validate/
      dashboard on 8137. All four watch processes completed initial cycles and stayed alive; SIGTERM
      shutdown was clean for workers. Full suite: 69/69. Repository excludes runtime DBs/logs/caches;
      autonomous `fintick-build` cron was already paused.

## Build config (now enforced at the Hermes level)
- **Local Qwen ONLY** — cloud `fallback_providers` is disabled; **do not** expect or rely on a
  fallback. If the local model errors, mark the item errored and continue.
- Streaming is on, so long aggregation turns won't be killed by the old non-stream timeout.
- Everything else per AGENTS.md: offline-fixture-first, commit per milestone, no sudo (write the
  supervisor script), no `git push`, DONE + pause when §7 all pass.

## Notes / decisions
- 2026-08-24: pivoted v1 → v2. Kept ingest + storage; replaced dedup/enrich/research with
  aggregate/facts/validate. Reframed "sources" = external validation only; stream = one origin.

## Blockers
(none)
