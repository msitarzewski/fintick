# FinTick

> **Designed by Claude Opus 4.8 · Built by Qwen 3.8 27B via Hermes.**

FinTick is a local-first, always-on financial news tape. It captures the
high-signal `fintwitter.bsky.social` firehose, collapses repeated headlines,
enriches each event with a local Qwen model, and serves the result as a dark,
glanceable market dashboard.

No cloud AI. No paid market-data API. No triplicate headlines.

```text
┌─ FINTICK // LIVE ───────────────────────────────────────────────────────┐
│ ▼ BZ  BRENT SETTLES $92.17, DOWN 2.35%  ◆  NG $2.782/MMBTU  ◆  ... → │
└─────────────────────────────────────────────────────────────────────────┘
```

## Status

FinTick is under active autonomous construction. The repository foundation is
runnable; ingest, deduplication, enrichment, research, and the dashboard arrive
in successive milestone commits. `STATUS.md` is the current source of truth.

## Quick start

FinTick intentionally depends only on the Python standard library at runtime.
Python 3.11 or newer is required (development is exercised on Python 3.14).

```bash
python3 -m fintick doctor
python3 -m unittest discover -v
```

The offline fixture at `reference/feed_sample.json` is the first proving ground.
Live Bluesky access is used only after the offline ingest and dedup pipeline
passes.

## Intended runtime

Once the pipeline milestones land, the main commands will be separate,
restart-safe processes:

```bash
python3 -m fintick ingest --offline reference/feed_sample.json
python3 -m fintick enrich
python3 -m fintick research
python3 -m fintick serve
```

The production database lives at `data/fintick.db`. Runtime data, logs, caches,
and secrets are ignored by git. A supervisor installer will make the workers
survive reboots without baking machine-specific state into the repository.

## Architecture

```text
Bluesky public AppView
        │
        ▼
 ingest + exact normalized dedup ───────► SQLite (`data/fintick.db`)
                                               │
                          ┌────────────────────┼──────────────────┐
                          ▼                    ▼                  ▼
                   local Qwen enrich     bounded research    JSON + web UI
                   localhost:11434       related context     live tape
```

The boundaries are deliberately simple:

- **Ingest:** paginates to previously seen posts and inserts by immutable URI.
- **Dedup:** keeps every row for audit, but marks one canonical event for the tape.
- **Enrich:** processes one canonical headline at a time through local structured
  inference; malformed model output is isolated and retryable.
- **Research:** attaches cached context only to consequential stories.
- **Dashboard:** serves raw headlines immediately, then fills in intelligence as
  workers complete it.

## Why this collaboration is unusual

Claude Opus 4.8 designed the product and acceptance contract. Qwen 3.8 27B,
running locally through Hermes, is building it milestone by milestone. Michael
sets the taste and the operational constraints. The result is meant to be less
of an AI demo and more of a small, legible system worth leaving on all day.

## Development rules

- Build and test against the captured fixture before touching the live API.
- Prefer Python's standard library and readable components.
- Keep ingest idempotent and retain duplicate rows for audit.
- Never commit the database, logs, caches, credentials, or secrets.
- Do not enrich duplicate headlines.

See `PRD.md` for the product contract, `AGENTS.md` for build rules, and
`STATUS.md` for milestone progress.

## License

MIT © 2026 Michael Sitarzewski.
