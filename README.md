# FinTick

> **Designed by Claude Opus 4.8 · Built by Qwen 3.8 27B via Hermes.**

FinTick watches one unusually fast financial stream and measures the gap between **the stream said it** and **the news confirms it**.

It drains every stream post into an auditable decision, merges repeats into one event, extracts concrete claims, hunts independent news for corroboration, and makes the valuable state obvious:

> 🔴 **BREAKING — no corroboration yet** means the stream may be ahead of the wire. It is a result, not an error.

No paid market-data API. No copied provider credentials. No pretending four repeated posts are four sources.

## The Edge Board

The web interface is an auto-refreshing event board—not a raw-post reader:

- Validation state is the visual focal point.
- Breaking, uncorroborated events sort first.
- Stream repetition is shown as `via the stream · seen N×`.
- “Sources” means only independent external stories with URLs.
- Confirmed events show how long the news lagged the stream.
- Pipeline health shows whether every post is caught up, retrying, or terminally errored.

Open **<http://127.0.0.1:8137>** after starting the dashboard.

## Quick start

FinTick is standard-library Python and requires Python 3.11 or newer. Aggregation defaults to GPT-5.6 Luna through Hermes-managed `openai-codex` OAuth; the one-shot invocation uses safe mode and Hermes' valid empty toolset, so untrusted stream text cannot invoke agent tools. Validation uses the common.vision Partner API when `FINTICK_COMMON_VISION_TOKEN` is configured; its index includes Google News feeds. Direct Google News RSS remains the no-token compatibility path. An Ollama-compatible local aggregation provider remains available at `http://localhost:11434`.

```bash
python3 -m fintick doctor
python3 -m unittest discover -s tests -v
```

### One-command fixture demo

```bash
./run-demo.sh
```

This ingests the canonical four-post NVDA cluster with an offline fixture response and starts the Edge Board on port 8137.

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

The unattended worker cadence is:

- ingest every 15 minutes,
- aggregate the oldest 50 pending posts, checking for more work every minute; retry groups are isolated
  and each post ends as assigned, ignored, or visibly errored,
- validate/revalidate eligible events every 5 minutes, including periodic repair of historical
  confirmations that no longer meet the current claim-overlap standard,
- refresh the browser every 20 seconds.

## Architecture

```text
fintwitter.bsky.social (one stream; the signal)
                       │
                       ▼
              durable URI ingest
                       │
                       ▼
       oldest-pending accounting ledger
           short-ID model contract
       Luna via Hermes OAuth (default)
            local model optional
                       │
                       ▼
          events ←→ event_signals
                       │
                       ▼
       common.vision Partner API validation
       (Google News + other indexed feeds)
            direct RSS when unconfigured
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
- `post_aggregation_decisions` records pending, assigned, ignored, retrying, and terminal outcomes.
- `events` stores the canonical event, facts, instruments, status, and lead time.
- `event_signals` maps repeated stream posts to their one event.
- `event_validations` stores external stories. These—and only these—are sources.

Aggregation is idempotent on a deterministic event key and never overwrites validation state. Validation is re-runnable: an event can begin `breaking` and flip to `confirmed` when the wire catches up, while stale or unrelated historical candidates are reclassified or removed before status is recalculated.

## Unattended operation

The installer writes four rootless user-systemd units—ingest, aggregate, validate, and dashboard. It discovers the checkout and executables at install time and refuses to overlap an existing process manager:

```bash
./setup-fintick-services.sh --dry-run
./setup-fintick-services.sh
systemctl --user status 'fintick-*'
```

The generated units optionally read `%h/.config/fintick/environment`. To enable common.vision validation, create that file with mode `0600` using a secure editor and add one line named `FINTICK_COMMON_VISION_TOKEN`; do not put the value in the repository or shell history. The dashboard binds to loopback on port 8137. Logs go to the user journal. Runtime databases, caches, provider state, and secrets are excluded by `.gitignore`.

### Model benchmark

The fixed 28-post quality corpus runs through the same short-ID parser used in production:

```bash
python3 -m fintick.benchmark --provider hermes --model gpt-5.6-luna
python3 -m fintick.benchmark --provider local --model YOUR_OLLAMA_MODEL
```

The command prints aggregate metrics only; raw model responses are not written to the repository.

## Product contract

- `PRD.md` defines the product and acceptance criteria.
- `STATUS.md` records verified milestone state.
- `reference/nvda_repost_cluster.json` is the canonical v2 acceptance fixture.
- `AGENTS.md` defines the current engineering, integrity, and rootless operations rules.

## The collaboration

Claude Opus 4.8 designed the product and acceptance contract. Qwen 3.8 27B, running locally through Hermes, built it milestone by milestone. Michael set the taste, reframed the stream as signal rather than “sources,” and kept the work honest: runnable systems over AI theater.

## License

MIT © 2026 Michael Sitarzewski.
