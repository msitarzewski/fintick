# AGENTS.md — build guide for the Hermes agent (local Qwen 3.8 27B)

You are building **FinTick** (see `PRD.md`), autonomously, on the machine `pipx`, using the
local Qwen 3.8 27B model via Hermes. You may run across **multiple cron fires** — that is
expected. Treat `STATUS.md` as your memory between fires.

## How to work across fires (IMPORTANT)

1. **First thing every fire:** read `STATUS.md`. It tells you which milestone you're on and what's
   left. Do NOT restart from scratch.
2. Advance the build by one meaningful milestone (or continue the current one), then **update
   `STATUS.md`**: tick completed items, set the current milestone, note anything learned or blocking.
3. **git commit** your progress at the end of each fire with a clear message
   (`git add -A && git commit -m "..."`). Commit early, commit often.
4. When **every acceptance criterion in PRD.md §7 passes**:
   - Write `DONE` as the very first line of `STATUS.md` with a one-paragraph summary.
   - **Pause this cron job** so it stops firing: `hermes cron pause fintick-build` (the job name).
   - Deliver a final report: what you built, how to run it, what (if anything) you couldn't finish.
5. If you hit a hard blocker you cannot resolve, write it clearly under "BLOCKERS" in `STATUS.md`
   and in your delivered message — do not spin silently.

## Environment (facts — verified)

- OS: Ubuntu 26.04. User: `michael`. Workdir: this directory (the repo root).
- **Python 3.14** is the system python3. Prefer the **standard library** (urllib, sqlite3,
  http.server, json, hashlib, re, subprocess). Reason: py3.14 is very new and many third-party
  wheels don't build yet — stdlib keeps the system runnable with zero install friction. If you
  truly need a package, prefer one that's pure-Python and pin it; never let a failed pip install
  block the build.
- `sqlite3` CLI is NOT installed, but Python's `sqlite3` module is — use it.
- `node` v22 and `npm` are available if you prefer JS for any piece (the dashboard, say). Your call.
- **Local model endpoint:** `http://localhost:11434` (Ollama, loopback only).
  - OpenAI-compatible: `POST http://localhost:11434/v1/chat/completions`
  - Native (supports forced JSON): `POST http://localhost:11434/api/chat` with `"format":"json"`
    or a JSON schema in `format`, and `"stream":false`.
  - Model name: **`qwen3.8:27b`**.
  - ⚠️ It is a **thinking model**: it emits `<think>…</think>` before its answer, and if you cap
    tokens too low it spends them all thinking and returns empty content. Give generous
    `max_tokens`/`num_predict`, and **strip `<think>…</think>` before parsing**. For structured
    extraction, use `/api/chat` with `"format":"json"` and low temperature — it constrains output
    to JSON so nothing leaks. Details + a worked description in `reference/local_model.md`.
- **Web/browser tools:** you have them (for PRD F4 research). The public Bluesky API needs no auth.

## Hard rules

- **NO sudo.** Do not attempt privileged commands. For the 24/7 process requirement, WRITE a
  script named `setup-fintick-supervisor.sh` that Michael will run himself (he has the sudo).
  It should create `/etc/supervisor/conf.d/*.conf` program entries (one per long-running process,
  `user=michael`, `autostart=true`, `autorestart=true`, correct `command=` and `directory=`) and
  end by printing the two commands he must run: `sudo supervisorctl reread && sudo supervisorctl
  update`. Model it on the other services on this box if you can read them, but do not require sudo
  to inspect them. **You** self-test by launching the processes yourself with
  `setsid nohup <cmd> >logs/<name>.log 2>&1 &` under your own user, confirming they stay up.
- **Build & test OFFLINE first** using `reference/feed_sample.json`. Only touch the live API for a
  final smoke test, and be polite to it (a handful of requests, back off on HTTP 429).
- **Dedup is mandatory** — the ticker is useless with triplicate headlines. See
  `reference/dedup_insight.md`.
- **Do not commit** the database, logs, caches, or any secret. Add them to `.gitignore`.
- **Do not `git push`** — no credentials are configured and you weren't asked to. Leave the repo
  clean and push-ready; Michael will push it.
- Keep the whole thing runnable at every commit. Never leave the tree in a broken state at the end
  of a fire.

## Repo & provenance

- Initialize git in this directory if not already (`git init`), branch `main`.
- `README.md` must prominently carry: **"Designed by Claude Opus 4.8 · Built by Qwen 3.8 27B via
  Hermes."** Make the README genuinely good — what FinTick is, a screenshot/ASCII of the tape, how
  to run it, the architecture, and the collaboration story.
- Add an MIT `LICENSE` (copyright holder: Michael Sitarzewski, 2026).

## Definition of good

Small, legible, dependency-light code that a human can read; resilient to bad model output and
network hiccups; idempotent; and a dashboard that genuinely feels like a live financial tape.
Favor finishing the core loop end-to-end over polishing any single part.

---

## Headless tooling — CONFIRMED constraints (read this)

You run in a **headless cron context** with no human present to approve tool calls. Verified on the first build fire:

- **`execute_code` is BLOCKED here** (it runs arbitrary Python that bypasses shell-string approval, which is disallowed without an approver). Do **not** call it — you will just waste a turn. Use the **`terminal`** tool to run everything: `python3 script.py`, `git`, `curl`, etc. The `terminal` tool works fine.
- **Browser / CDP tools are UNAVAILABLE** in this context. For PRD F4 ("related stories" research), do the lookup from the **`terminal`** tool with **`curl`** against a free source (a public news/search JSON API, or an RSS feed you parse with stdlib) — not a browser. If no research path is reachable at build time, implement the code path anyway and cover it with a **stubbed test** (acceptance criterion #4 explicitly allows this), and note the limitation in STATUS.md.
- Everything else (read_file, write_file, search_files, terminal) is available and working.
