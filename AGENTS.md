# AGENTS.md — engineering and operations guide for FinTick

FinTick is an operating financial event-intelligence pipeline, not a greenfield build. `PRD.md`
defines the product contract; `STATUS.md` records the current release, deployment, and blocker
state. Read both before changing behavior, and inspect current repository and runtime state rather
than assuming a particular machine, provider, or service manager.

## Working across sessions

1. Read `STATUS.md`, inspect `git status`, and continue the current milestone rather than restarting.
2. Use tests and copied operational data before mutating the live database or workers.
3. Update `STATUS.md` when release state, verified behavior, or blockers materially change.
4. Commit only at a coherent, independently reviewed boundary with all relevant gates passing.
5. Never `git push` unless Michael explicitly asks. Leave the repository clean and push-ready.
6. Report hard blockers directly; do not silently spin or describe unverified work as complete.

## Runtime and inference

- FinTick supports Python 3.11+ and is standard-library-first (`urllib`, `sqlite3`, `http.server`,
  `json`, `hashlib`, `re`, `subprocess`). Verify the active interpreter before making host claims.
- The production aggregation default is Hermes-managed `openai-codex` OAuth with
  `gpt-5.6-luna`. Authentication belongs to the user's external Hermes state: never read, copy,
  print, store, or commit it from FinTick.
- Inference remains provider-injected and offline-testable. The Ollama-compatible local endpoint at
  `http://localhost:11434` is supported for development and benchmarking, not assumed to be the
  production provider. See `reference/local_model.md` for local thinking-model handling.
- Compare providers through the same checked-in benchmark corpus, short-ID contract, parser, and
  scorer. Do not weaken accounting rules to accommodate a model that fails the contract.
- Public Bluesky access requires no authentication. Be polite to public APIs and back off on 429.

## Post-accounting integrity

- Preserve every ingested post by immutable URI; do not hash-deduplicate posts out of the stream.
  Semantic repetition is merged at the event layer and remains useful `seen N×` evidence.
- Process the oldest pending posts chronologically.
- Every selected short post ID must receive exactly one durable outcome: assigned to one event,
  intentionally ignored with a reason, retryably errored, or terminally errored after bounded
  attempts.
- A post URI may belong to at most one event. Ambiguous ownership fails closed.
- Retry failed subsets as their original group so one malformed batch neither contaminates nor
  blocks unrelated fresh work.
- Terminal post decisions are immutable and idempotent. Preserve additive, non-destructive database
  migrations and v1 compatibility.

## Validation integrity

- When `FINTICK_COMMON_VISION_TOKEN` is configured, use the common.vision Partner API as the single
  validation index; it already includes Google News feeds. Without a token, retain direct Google
  News RSS compatibility. Never commit or print the partner token.
- Social-network post URLs are stream/social signals, not independent corroborating sources.
- RSS/search results are untrusted candidates. Entity overlap alone is not corroboration.
- Candidate titles must match the event's claim context and, where available, its fact values.
- Periodically revalidate confirmed events. Reclassify or remove historical candidates that no
  longer satisfy the current standard before recalculating event status.
- Use the earliest valid corroborating timestamp for lead time. Malformed timestamps do not count.
- An empty successful search produces a `breaking` result; a network/parser failure is an error.

## Rootless service operations

- **No sudo.** Follow the host's established rootless `systemd --user` convention.
- Maintain `setup-fintick-services.sh`, with one unit each for ingest, aggregate, validate, and the
  dashboard. Tracked source must not hard-code usernames, home directories, machine names, or
  operator-specific hosts; generated local units may contain discovered absolute paths.
- The installer must support dry-run/preflight checks and refuse to overlap another FinTick manager
  or listener. Stop the old manager before enabling the replacement.
- Verify service ownership and parentage, `systemctl --user` state, user-journal logs, loopback API
  health, operational database identity, pipeline accounting, and backlog movement after handoff.
- Preserve a known-good database backup and rollback path before any live cutover.

## Repository and release rules

- Build and test offline first with checked-in fixtures. Only then use bounded live smoke tests.
- Do not commit databases, logs, caches, credentials, Hermes/Codex auth state, or secrets. Keep them
  in `.gitignore`.
- Keep the repository runnable at every commit. Run the full tests, compile checks, shell syntax,
  `git diff --check`, security scan, copied-live invariants, and independent review before release.
- Preserve the README provenance: **"Designed by Claude Opus 4.8 · Built by Qwen 3.8 27B via
  Hermes."** Preserve the MIT license (Michael Sitarzewski, 2026).

## Definition of good

Small, legible, dependency-light code that a human can read; resilient to malformed model output,
network hiccups, retries, restarts, and bursty intake; auditable post accounting; conservative
validation; idempotent operation; and an Edge Board that exposes real pipeline health rather than
merely claiming to be live.

## Headless cron execution profile

The original autonomous build ran in a headless cron context where `execute_code` and browser/CDP
tools were unavailable. Those constraints apply only when the current execution surface confirms
them. Interactive Hermes Desktop sessions may have browser and additional tools. Always inspect
actual availability; regardless of surface, keep model/network paths dependency-injected and cover
them with offline tests.
