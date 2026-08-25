V2.1 RELEASE CANDIDATE — LIVE SERVICE HANDOFF PENDING

# FinTick — Build Status (v2.1: accountable ingestion)

FinTick v2.1 is a verified release candidate: every new post enters a durable accounting ledger;
aggregation drains oldest pending work through short IDs; every selected ID must become one event signal,
an explicit ignore, or a visible bounded error; failed subsets retry together. GPT-5.6 Luna is reached
through Hermes-managed OAuth, with the local provider retained. The Edge Board exposes backlog and error
health. All 143 tests, compile checks, shell syntax, and diff checks pass. Luna scored 100% accounting on
the fixed 28-post corpus. Copied-live catch-up reached zero backlog with isolated retries exercised.
Copied-live validation repair reduced unsupported confirmations from 20 to 7 and retained only
claim-aligned evidence. The live Supervisor workers remain unchanged until the rootless user-systemd
handoff is approved.

<!-- When ALL PRD.md §7 acceptance criteria pass, replace the line below with `DONE` + a
     one-paragraph summary, then run: hermes cron pause fintick-build -->
STATUS: RELEASE CANDIDATE — DEPLOYMENT PENDING

**Current milestone:** M7 — independent review, documentation, and service-manager handoff.

## The v2 pivot (read this first every fire)
v1 is tagged **`v1-baseline`** (a working per-post ticker) and its acceptance polish is stashed
(`git stash list` → "abandoned-m8-wip"). **Do NOT resume v1.** v2 **keeps v1's ingest + storage**
and **replaces the middle**: the old `dedup → enrich → research` becomes
**`aggregate → facts → validate`** (see PRD.md, rewritten). The core idea: the feed is **one
stream** (the signal, never "sources"); merge its repeats into one event; hunt **external** news to
validate; and an event with **0 external sources = `breaking`**, which is the point, not a failure.

Canonical test fixture: `reference/nvda_repost_cluster.json` — the four NVDA posts must become
**one** event, and offline must land as **`breaking`** (0 external sources).

> The v1 hard-rule "Dedup is mandatory" is **retired** for v2 — do NOT hash-dedup. Aggregation (F2)
> is the merge mechanism. `AGENTS.md` now reflects this accountable-ingestion contract.

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
      Live `qwen3.8:27b` verified 2026-08-25 against the canonical fixture: one event, four signals,
      unified NVDA, and three facts including the −3.2% move.
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
      shutdown was clean for workers. Full suite: 80/80. Repository excludes runtime DBs/logs/caches;
      autonomous `fintick-build` cron was already paused.

## Post-acceptance integrity hardening (2026-08-25)

- RSS search results are candidates, not automatic corroboration: title-level entity and claim overlap
  classifies them conservatively as corroborating, partial, disputing, or unrelated.
- Confirmed-event lead time uses the earliest parseable corroborating publication.
- Existing stream-signal membership wins over model headline wording, preventing duplicate events when
  rolling passes rephrase the same event while preserving validation state. Candidates bridging multiple
  existing events are rejected and isolated; SQLite enforces one-event ownership for future signal links.
- Factless model events are rejected independently. Live Google News RSS validation moved the isolated
  NVDA smoke event from `breaking` to `confirmed` using an on-claim seventh-session story.
- Live operational tuning against `data/fintick.db` established 10 posts as the reliable default batch:
  one uncontended pass selected 10 real posts, created 5 events, and reported 0 errors in about 100
  seconds. The six-hour window retains its explicit 200-post cap. Qwen aggregation disables reasoning
  and caps JSON output at 4096 tokens to avoid wasting the request timeout on hidden reasoning.

## v2.1 accountable ingestion (2026-08-25)

- Additive `post_aggregation_decisions` ledger: `pending`, `assigned`, `ignored`, `errored`, and
  migration-only `out_of_scope`; the six-hour bootstrap boundary avoids pretending old history was reviewed.
- Oldest-pending batches replace newest-N sampling. Short IDs remove URI-copy failures. The parser rejects
  missing, duplicate, unknown, or multiply assigned IDs and requires explicit ignore reasons.
- Provider failures and rejected posts retry at most three times. Durable retry groups preserve the exact
  failed subset instead of mixing unrelated failures or fresh backlog, even when the original group exceeds
  a later caller's fresh-batch limit. Pending and health ordering compare UTC instants rather than ISO text.
- Default provider is GPT-5.6 Luna through the existing Hermes `openai-codex` OAuth state. FinTick neither
  reads nor stores credentials. The one-shot subprocess uses safe mode plus Hermes' valid empty toolset, so
  untrusted stream text cannot invoke agent tools. Local Ollama remains dependency-injected for offline tests
  and benchmarks.
- Fixed benchmark rerun: Luna accounted for 28/28 posts in 47.2 seconds with no parser errors,
  perfect expected-pair precision, and 0.6538 expected-pair recall (10 events, 2 explicit ignores,
  42 facts). The result is contract-correct but more fragmented than the earlier sample. Installed
  local MoE returned no decisions under the strict contract and remains non-production.
- Operational copied-live smoke under a minimal service environment completed a 50-post Luna batch. A retry
  defect found there was reproduced, fixed, and covered by tests.
- Final copied-live catch-up accounted for all 1,006 posts: 283 assigned, 45 explicitly ignored, and 678
  migration-scoped historical posts. Backlog, retrying, terminal errors, and duplicate signal owners all
  reached zero. One failed 50-post Luna group retried successfully in isolation before fresh work resumed.
- Confirmed events are now periodically revalidated. Historical source titles are reclassified through the
  current conservative matcher and unrelated candidates are removed before status is recalculated. On a fresh
  operational database copy with network lookup disabled, this repaired 20 historical confirmations to 7
  claim-aligned confirmations, 3 developing events, and 24 breaking events; stored candidates fell from 73 to 18.
- External candidate stances are never trusted to bypass title-level claim matching. Lead-time calculation
  requires timezone-aware event and publication timestamps, preventing host-timezone-dependent wire lag.
- A bounded live-RSS smoke exposed one generic live-blog false positive (`US`, `Trump`, `removed`). A red
  regression now requires claim-specific evidence spanning context and fact values; the rerun removed that row,
  retained three on-claim Hormuz corroborations, and completed five validations with zero errors.
- common.vision Partner API validation is selected when `FINTICK_COMMON_VISION_TOKEN` is configured; its
  index already includes Google News feeds, so FinTick does not double-query direct Google RSS. Searches are
  date-bounded and use broad two-anchor retrieval followed by the same conservative claim classifier. Social
  URLs are excluded as independent sources. A copied-live five-event smoke completed with zero errors and did
  not promote any weak candidates. The token lives outside the repository in a mode-0600 user environment file.
- The supplied Partner API OpenAPI 3.0.3 contract is now captured in
  `reference/common_vision_partner_api.md`. Article `metadata.source` is stored as the publisher while
  `feed.name`, `feed.url`, and `feed.feed_type` are preserved separately through SQLite and `/api/feed`.
  Existing databases gain those columns additively. HTTP 429 honors either `Retry-After` or the documented
  JSON `retry_after`; 401/403/404/422/500 and malformed envelopes remain fail-closed validation errors.
- Independent adversarial review exposed and drove regressions for three accounting/evidence defects: exhausted
  errors are now immutable, legacy-compatible model responses account for assigned and omitted posts, and the
  final validation-source boundary removes social URLs during both fresh lookup and historical revalidation.
  Social hosts are matched on normalized URL hostnames, including port, userinfo, subdomain, and trailing-dot
  disguises.
- `setup-fintick-services.sh` follows this host's established rootless user-systemd pattern. It rejects both
  running workers, dormant FinTick Supervisor definitions, independent port collisions, and insecure
  validation environment-file metadata; it
  preserves WAL-safe database and prior unit-state snapshots, verifies exact-file API/database identity,
  complete accounting, backlog movement, and unit liveness before and after bounded API polling. Failed
  verification first proves every replacement unit inactive, then restores the database without stale
  sidecars plus prior enabled/active unit state. Failed final replacement recovers the original main/WAL/SHM
  family. Fresh installs wait for service activation to create the operational database before capturing
  exact file identity. Prior writers remain stopped unless database recovery, every unit artifact, and the
  user-systemd daemon reload all succeed. Prior and final unit states are
  explicit recognized categories; unknown or transitional states abort or mark rollback incomplete.
  Persistent and runtime unit masks are snapshotted, removed before replacement activation, and restored
  exactly on rollback. Process preflight recognizes Python options and the installed console entrypoint.
  Stop or restoration failures are reported explicitly
  without claiming success, and the private snapshot remains available; both successful and failed handoffs
  retain bounded journal evidence. It replaces the obsolete Supervisor
  installer but has not yet been activated while the old root-owned workers are running.
- `AGENTS.md` now defines provider-injected inference, durable accounting, conservative revalidation, and
  rootless user-systemd operations rather than preserving the historical local-Qwen/Supervisor build mandate.
- Current verification: 143/143 tests, `compileall`, `bash -n`, and `git diff --check` pass.

## Notes / decisions
- 2026-08-24: pivoted v1 → v2. Kept ingest + storage; replaced dedup/enrich/research with
  aggregate/facts/validate. Reframed "sources" = external validation only; stream = one origin.

## Blockers

- One-time operator handoff required after review: stop/remove the existing root-owned Supervisor FinTick
  programs. Then install and verify the four rootless user-systemd units. No live source/database cutover
  should occur before that controlled handoff.
