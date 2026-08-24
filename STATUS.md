# FinTick — Build Status

<!-- When the build is fully complete and all PRD §7 acceptance criteria pass,
     replace the line below with `DONE` followed by a one-paragraph summary,
     then run: hermes cron pause fintick-build -->
STATUS: IN PROGRESS

**Current milestone:** M5 — bounded related-story research and caching.

## Milestones (tick as you finish; keep the tree runnable at every commit)

- [x] **M1 — Repo skeleton.** git init (branch main), README (with provenance line), LICENSE (MIT),
      .gitignore (data/, *.db, logs/, caches, __pycache__). Directory layout decided.
- [x] **M2 — Ingest + storage.** Fetch author feed (offline via reference/feed_sample.json first),
      SQLite schema, store raw posts idempotently (uri = PK). Pagination-to-last-seen.
- [x] **M3 — Dedup.** Normalize + hash + windowed collapse of ALLCAPS/Title-Case reposts;
      canonical vs duplicate rows. Verify it collapses the known dupes in the fixture.
- [x] **M4 — Enrich (local Qwen).** Structured tagging via localhost:11434: summary, category,
      importance, sentiment, instruments (global symbols), entities, regions. Resilient to bad output.
- [ ] **M5 — Research.** Web lookup of related stories for importance ≥ 3 items; attach links; cache.
- [ ] **M6 — Dashboard.** Auto-refreshing tape + enriched cards; dark terminal aesthetic; symbol chips.
- [ ] **M7 — Run-as-process.** Long-lived processes; self-test with setsid/nohup; write
      setup-fintick-supervisor.sh for Michael. Live smoke test against the real API.
- [ ] **M8 — Acceptance pass.** Walk PRD §7 items 1–7; confirm each; polish README with a real
      screenshot/ASCII of the running tape.

## Notes / decisions
- 2026-08-24 / M1: Standard-library Python package with `unittest`; keep one CLI with
  separate ingest/enrich/research/serve subcommands as those processes land. SQLite runtime state
  will live in ignored `data/fintick.db`. Baseline verified on Python 3.14 with compileall, one CLI
  test, and `python3 -m fintick doctor`.
- Offline fixture shape verified: 60 newest-first author-feed entries plus an opaque cursor; each
  entry wraps `post`, with source text/timestamp under `post.record`.
- 2026-08-24 / M2: Added a WAL-mode SQLite store keyed by post URI, retained compact raw JSON and
  source metadata, and persisted newest URI/timestamp high-water marks. The stdlib AppView client
  builds unauthenticated `posts_no_replies` requests and the paginator stops at an entirely known
  page or an eight-page cap. Offline CLI verification inserted 60 posts on the first run and 0 on
  the second; six unit tests pass, including multi-page traversal and high-water correctness.
- 2026-08-24 / M3: Added lowercase/whitespace/trailing-punctuation normalization, stable SHA-1
  hashes, and deterministic 60-minute clustering anchored on the earliest chronological post.
  Duplicates remain auditable and link directly to canonical rows; existing M2 databases migrate
  and backfill transactionally. Offline fixture verification yields exactly 60 stored / 55
  canonical / 5 duplicate rows and is idempotent on rerun. Sixteen tests pass, including reverse
  insertion order, overlapping-window non-chaining, timezone-offset ordering, exact boundary,
  malformed legacy data, migration, and canonical-link invariants. Independent review passed.
- 2026-08-24 / M4: Added one-headline-per-call enrichment through Ollama's native forced-JSON
  endpoint, with strict validation for summary/category/importance/sentiment/instruments/entities/
  regions, thinking-wrapper cleanup, per-item error isolation, and a three-attempt cap. Canonical
  work is atomically leased with fenced UUID tokens, so concurrent or stale workers cannot corrupt
  completed results; abandoned leases become retryable after 15 minutes. Offline stubs cover bad
  JSON, missing structure, retries, canonical-only selection, concurrency, and stale-worker races.
  A real local `qwen3.8:27b` smoke test produced a complete NVDA enrichment from the fixture.
  Twenty-six tests pass; repeated concurrency checks and independent pre-commit review passed.

## Blockers
(none)
