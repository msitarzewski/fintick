# FinTick

> **Designed by Claude Opus 4.8 · Built by Qwen 3.8 27B via Hermes.**

FinTick watches one unusually fast financial stream and measures the gap between **the stream said it** and **the news confirms it**.

It merges repeated stream posts into one event, extracts the concrete claims with a local model, hunts independent news for corroboration, and makes the valuable state obvious:

> 🔴 **BREAKING — no corroboration yet** means the stream may be ahead of the wire. It is a result, not an error.

No cloud AI. No paid market-data API. No pretending four repeated posts are four sources.

## The Edge Board

The web interface is an auto-refreshing event board—not a raw-post reader:

- Validation state is the visual focal point.
- Breaking, uncorroborated events sort first.
- Stream repetition is shown as `via the stream · seen N×`.
- “Sources” means only independent external stories with URLs.
- Confirmed events show how long the news lagged the stream.

Open **<http://127.0.0.1:8137>** after starting the dashboard.

## Quick start

FinTick is standard-library Python and requires Python 3.11 or newer. The local inference endpoint is Ollama-compatible at `http://localhost:11434`, with model `qwen3.8:27b`.

```bash
python3 -m fintick doctor
python3 -m unittest discover -s tests -v
```

### One-command fixture demo

```bash
./run-demo.sh
```

This ingests the canonical four-post NVDA cluster, aggregates it with local Qwen, and starts the Edge Board on port 8137.

### Run the v2 pipeline manually

```bash
# One-shot proving run
python3 -m fintick ingest --fixture reference/nvda_repost_cluster.json
python3 -m fintick aggregate
python3 -m fintick validate
python3 -m fintick serve

# Continuous workers—run each in a separate terminal
python3 -m fintick ingest --watch
python3 -m fintick aggregate --watch
python3 -m fintick validate --watch
python3 -m fintick serve --port 8137
```

The default worker cadence is:

- ingest every 15 minutes,
- aggregate one rolling six-hour window every 15 minutes (10 posts by default on the current local
  model; configurable with `--limit` up to the 200-post safety cap),
- validate/revalidate unconfirmed events every 5 minutes,
- refresh the browser every 20 seconds.

## Architecture

```text
fintwitter.bsky.social (one stream; the signal)
                       │
                       ▼
              durable URI ingest
                       │
                       ▼
          rolling 6h aggregate + facts
             one local Qwen call
                       │
                       ▼
          events ←→ event_signals
                       │
                       ▼
       independent RSS/news validation
                       │
          ┌────────────┼──────────────┐
          ▼            ▼              ▼
      BREAKING      CONFIRMED      DISPUTED /
    no sources yet  external URLs   DEVELOPING
          └────────────┴──────────────┘
                       │
                       ▼
           JSON API + Edge Board :8137
```

The SQLite database lives at `data/fintick.db`:

- `posts` retains every stream post by immutable URI.
- `events` stores the canonical event, facts, instruments, status, and lead time.
- `event_signals` maps repeated stream posts to their one event.
- `event_validations` stores external stories. These—and only these—are sources.

Aggregation is idempotent on a deterministic event key and never overwrites validation state. Validation is re-runnable, so an event can begin `breaking` and flip to `confirmed` when the wire catches up.

## Unattended operation

The installer writes four Supervisor programs—ingest, aggregate, validate, and dashboard—and runs FinTick as the unprivileged `michael` user:

```bash
sudo ./setup-fintick-supervisor.sh
sudo supervisorctl reread
sudo supervisorctl update
```

The dashboard binds to loopback on port 8137. Runtime databases, logs, caches, and secrets are excluded by `.gitignore`.

## Product contract

- `PRD.md` defines the product and acceptance criteria.
- `STATUS.md` records verified milestone state.
- `reference/nvda_repost_cluster.json` is the canonical v2 acceptance fixture.
- `AGENTS.md` describes the local-first build constraints.

## The collaboration

Claude Opus 4.8 designed the product and acceptance contract. Qwen 3.8 27B, running locally through Hermes, built it milestone by milestone. Michael set the taste, reframed the stream as signal rather than “sources,” and kept the work honest: runnable systems over AI theater.

## License

MIT © 2026 Michael Sitarzewski.
