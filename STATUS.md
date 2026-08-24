# FinTick — Build Status

<!-- When the build is fully complete and all PRD §7 acceptance criteria pass,
     replace the line below with `DONE` followed by a one-paragraph summary,
     then run: hermes cron pause fintick-build -->
STATUS: IN PROGRESS

**Current milestone:** M2 — ingest + SQLite storage, built and tested offline against the 60-post fixture.

## Milestones (tick as you finish; keep the tree runnable at every commit)

- [x] **M1 — Repo skeleton.** git init (branch main), README (with provenance line), LICENSE (MIT),
      .gitignore (data/, *.db, logs/, caches, __pycache__). Directory layout decided.
- [ ] **M2 — Ingest + storage.** Fetch author feed (offline via reference/feed_sample.json first),
      SQLite schema, store raw posts idempotently (uri = PK). Pagination-to-last-seen.
- [ ] **M3 — Dedup.** Normalize + hash + windowed collapse of ALLCAPS/Title-Case reposts;
      canonical vs duplicate rows. Verify it collapses the known dupes in the fixture.
- [ ] **M4 — Enrich (local Qwen).** Structured tagging via localhost:11434: summary, category,
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

## Blockers
(none)
