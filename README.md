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

FinTick's end-to-end pipeline is operational: durable Bluesky ingest, auditable
deduplication, local-model enrichment, bounded related-story research, and the
live dashboard. `STATUS.md` tracks the final acceptance pass.

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

## Run it

```bash
# Offline proving run (never contacts Bluesky)
python3 -m fintick ingest --fixture reference/feed_sample.json

# Start the continuous workers (separate terminals)
python3 -m fintick ingest --watch
python3 -m fintick enrich --watch
python3 -m fintick research --watch
python3 -m fintick serve
```

Open <http://127.0.0.1:8080>. The worker intervals are configurable with
`--interval`; ingest defaults to 15 minutes. Each worker runs an immediate cycle,
logs counts to stdout, survives transient cycle failures, and exits cleanly on
SIGTERM/SIGINT. Continuous enrichment and research deliberately process one item
per cycle so shutdown never sits behind a claimed batch; one-shot commands still
honor `--limit` for bulk work.

For unattended operation across reboots, install the four Supervisor programs:

```bash
sudo ./setup-fintick-supervisor.sh
sudo supervisorctl reread && sudo supervisorctl update
```

The production database lives at `data/fintick.db`. Runtime data, logs, caches,
and secrets are ignored by git. FinTick itself runs as the unprivileged `michael`
user; the installer needs root only to write Supervisor configuration.

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
