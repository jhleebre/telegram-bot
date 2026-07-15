# Phase 2 — Development Plan (not yet implemented)

Phase 2 adds the LLM/VLM-powered pipelines on top of the Phase 1 skeleton. The engine is
**Claude Code in headless mode** (`claude -p`), and multi-turn human-in-the-loop review is built
on **Claude Code resumable sessions** (`--session-id` / `--resume`). The routing table and stub
handlers from Phase 1 mean Phase 2 mostly fills in handler bodies and adds a few new modules.

**Ingestion recap (from the Phase 1 hybrid):** input files arrive via **Saved Messages** (Telethon)
and are downloaded to a temp dir with `message.download_media(...)`. The **review conversation runs
in the bot DM** (not Saved Messages, to avoid the self-ingest loop): while a review is pending, the
reply bot switches on `getUpdates` polling to receive the owner's answer, then goes idle again.

## Delivery approach — one feature at a time (IMPORTANT)

**Do NOT implement all of Phase 2 at once.** Add **one capability at a time**, and only move on
after the previous one is implemented, tested, and confirmed working by the owner in the real app.

For **each** increment:
1. Implement the smallest end-to-end slice of that one feature (plus any shared plumbing it needs).
2. Add/extend unit tests (mock `claude -p` / STT / VLM — no real model runs in the suite) and keep
   the whole suite green.
3. Manually verify in the running app (send a real message of that type; confirm the note/reply).
4. Update the docs, commit.
5. **Stop and get the owner's confirmation** before starting the next increment.

Recommended order (each is a self-contained milestone — ship and verify before the next):

- [x] **1. `claude -p` engine** (`engine/claude_cli.py`) — the shared subprocess wrapper + a trivial
      smoke use (e.g. LLM-enriched text title/tags). Nothing else depends on this until it works.
      **Shipped** — see "Increment 1 (as built)" below.
- [ ] **2. Document files → Markdown** (`document_handler` + `converters/` + `files/originals.py`) —
      no review loop; simplest file pipeline. Includes `.md` passthrough.
- [ ] **3. Image → described note** (`image_handler`, VLM via `claude -p`) — still no review loop.
- [ ] **4. Human-in-the-loop plumbing** (`core/session_store.py`, `handlers/conversation.py`, bot-DM
      polling) — build and test the review state machine on a simple case first.
- [ ] **5. Audio → meeting note** (`audio_handler`, `mlx-whisper`, glossary, review) — the most
      complex; depends on 1 and 4.

Rationale: features 2–3 are one-shot and low-risk, so they validate the engine (1) before the
harder two-way review flow (4) and the STT-heavy audio pipeline (5) are attempted.

## Scope

| Input | Output | Original file |
|-------|--------|---------------|
| Audio (m4a/wav/mp3/voice) | Meeting note (glossary-corrected, reviewed) → `0_inbox` | deleted after success |
| Image (screen capture) | Description + OCR note, original embedded → `0_inbox` | kept (embedded/`.assets`) |
| pdf / pptx / docx / xlsx / … | Converted Markdown → `0_inbox` | **moved to `~/Downloads/`** |
| Markdown (`.md`) | Saved as-is → `0_inbox` | is the note |
| Text (upgrade) | LLM-enriched title/tags/summary (fallback = Phase 1 path) | — |

## New modules

```
src/contextbot/
├── engine/
│   ├── claude_cli.py       # wrapper over `claude -p` (async subprocess, JSON parse)
│   └── prompts/            # prompt templates: meeting / image / doc-convert
├── core/
│   └── session_store.py    # per-chat conversation state + Claude session_id persistence
├── handlers/
│   ├── audio_handler.py    # (fill in) audio → meeting note
│   ├── image_handler.py    # (fill in) image → described note
│   ├── document_handler.py # (fill in) doc → markdown; .md passthrough
│   └── conversation.py     # routes a plain-text reply into an active pending session
├── converters/
│   └── doc_to_markdown.py  # deterministic base extraction for office/pdf docs
└── files/
    └── originals.py        # original-file policy (Downloads / delete / embed)
```

Additional runtime deps: `mlx-whisper` (STT, Apple Silicon); a base document converter
(candidate `markitdown`, or `pandoc` + `python-docx`/`python-pptx`/`pdfplumber`). `ffmpeg` is
already present. `claude` CLI is already installed (v2.1.187 verified).

## Engine: `claude -p` wrapper (`engine/claude_cli.py`)

- Invoke headlessly via `asyncio.create_subprocess_exec` (non-blocking; surfaces progress to the
  UI as PROCESSING):
  ```
  claude -p --output-format json --model <model> \
         --add-dir <workdir> --add-dir <glossary_dir> [--permission-mode <mode>]
  ```
- Parse the JSON result to capture **`session_id`** and the text output. Enforce a per-job
  timeout with cancellation.
- Resume for the next turn (keeps full prior context — transcript, draft, glossary):
  ```
  claude -p --resume <session_id> --output-format json
  ```

### Increment 1 (as built)

Delivered: `engine/claude_cli.py`, `engine/parsing.py`, `engine/prompts/` (+ `text_enrich.md`),
engine settings in `config.py`, and the smoke use — **item E, LLM-enriched text notes**.

Design points that differ from / firm up the sketch above:

- **The prompt goes on stdin, not argv.** `claude -p` reads its prompt from stdin when no prompt
  argument is given. Meeting transcripts and extracted documents can be large, and an argv-borne
  prompt would eventually hit `ARG_MAX`. Verified against the real CLI.
- **Runs are hermetic.** Every invocation adds `--strict-mcp-config --setting-sources ""
  --disable-slash-commands`, so the bot's output can't drift with the owner's personal Claude Code
  settings, MCP servers, or skills.
- **`--system-prompt` per job**, replacing the default agent prompt: the pipelines are extractors,
  not coding agents.
- **No permission mode is needed** (open decision 1, resolved → *bot writes*): the engine is
  text-in / text-out and Claude is never granted write access. `--add-dir` remains available for
  read-only context (the glossary, in increment 5).
- **Result JSON shape** (verified, CLI v2.1.187): `{result, session_id, is_error, subtype,
  total_cost_usd, duration_ms, num_turns}` → `ClaudeResult`. Errors surface as `ClaudeUnavailable`
  (no CLI), `ClaudeTimeout` (killed at the deadline), `ClaudeError` (non-zero exit / non-JSON /
  `is_error`).
- **`engine/parsing.extract_json_object`** tolerates ``` fences and prose around the JSON object,
  since "reply with JSON only" is not a guarantee.
- **Enrichment is an upgrade, never a dependency.** `handle_text` falls back to the Phase 1 path
  (first line as title, no tags) on *any* failure, and `CLAUDE_ENABLED=false` skips the engine
  entirely — the bot still captures notes offline. The note body is always the original text; the
  LLM only supplies frontmatter.

New settings (all optional, see `.env.example`): `CLAUDE_ENABLED` (default true), `CLAUDE_BIN`
(`claude`), `CLAUDE_MODEL` (`sonnet`), `CLAUDE_TIMEOUT_SEC` (`120`; text enrichment self-caps at
60s so a slow run can't stall ingestion of a plain memo).

#### Finding the CLI when launched from the Dock (found in owner verification)

The first real-app run silently fell back with `'claude' not found on PATH`, even though `claude`
worked fine in the terminal. A GUI app launched from the Dock/Finder inherits **launchd's minimal
PATH** (`/usr/local/bin:/bin:/usr/bin`) rather than the login shell's, and the `.app` launcher
`exec`s Python directly without a login shell — so Homebrew's `/opt/homebrew/bin` is invisible to
`shutil.which`. Development and tests, run from a terminal, could never have caught this.

`engine.claude_cli.resolve_executable` therefore falls back to the standard install locations
(`/opt/homebrew/bin`, `/usr/local/bin`, `~/.claude/local`, `~/.local/bin`) after a PATH lookup
fails; `CLAUDE_BIN` remains the escape hatch for anything unusual (e.g. an nvm/npm install).

The lesson generalizes past this one binary: **increment 5's `mlx-whisper` and `ffmpeg` will hit
exactly the same wall** — resolve them the same way rather than assuming PATH.

#### Usage limits / API errors (verified by injecting a 429)

Behaviour measured against CLI v2.1.187 by pointing `ANTHROPIC_BASE_URL` at a local endpoint
returning HTTP 429:

```json
{ "subtype": "success",         // ← stays "success" even on failure
  "is_error": true,             // ← the reliable signal
  "api_error_status": 429,      // ← the structured status
  "result": "API Error: Server is temporarily limiting requests (not your usage limit) · …" }
// exit code 1, empty stderr, fails in ~2s
```

Three consequences, all now handled:

- **`subtype` is not a failure signal** — `is_error` and the exit code are. The subtype check
  survives only as defense for result kinds that may not set `is_error` (e.g. max turns).
- **The detail lives in stdout, not stderr** — the CLI prints its JSON *and* exits non-zero, with
  stderr empty. Reporting only the exit code produced a useless `claude exited 1: <no stderr>`,
  indistinguishable from a network outage or a bad flag. `run()` therefore parses stdout first and
  only falls back to the exit code when there is no JSON.
- **429 is typed as `ClaudeUsageLimit`** (a `ClaudeError`, so existing callers still degrade).
  It covers both a temporary server-side rate limit and an exhausted subscription limit — the CLI
  reports both as 429 and distinguishes them only in the message, which is preserved.

For text notes this is fully safe: the note is saved via the Phase 1 path after a ~2s failure, and
the bot DM says why. **Increments 2–5 need more thought** — see "Degradation per pipeline" below.

#### Degradation per pipeline (decide when each increment is built)

Text enrichment degrades cleanly because a no-LLM path exists. **The later pipelines have no such
luxury and must not silently produce a bad note:**

| Pipeline | If the engine is unavailable / limited |
|----------|----------------------------------------|
| Text (1) | ✅ Phase 1 path; note keeps full fidelity, only metadata is weaker |
| Document (2) | Deterministic converter output can still be saved un-refined — degrade like text |
| Image (3) | No fallback: a described note *is* the LLM output. Save the image + a stub note? |
| Audio (5) | **No fallback.** STT is local, so the transcript survives — but the meeting note does not. Saving the raw transcript beats losing the recording. |
| Review (4) | A limit mid-review would strand a session in `AWAITING_REVIEW`. The state machine must handle "cannot resume right now" without dropping the draft. |

A **circuit breaker** (skip the engine for N minutes after a `ClaudeUsageLimit` instead of retrying
per message) is deliberately *not* built yet: at ~2s per failure and a few notes a day it buys
nothing. It becomes worthwhile once audio jobs — which are expensive and may retry — exist.

#### Making degradation visible

The fallback is deliberately silent in the note (that is the point — never lose a capture), which
also made a *broken engine* indistinguishable from a working one without reading the log. Two
surfaces now report it:

- The health panel carries a **`claude-engine`** probe showing the resolved path + model; a missing
  CLI reports **DEGRADED** (not ERROR — capture still works). It proves the binary is *findable*,
  not that it can *run* — a usage limit is invisible to it.
- The **bot DM reply** appends `⚠️ …(제목/태그는 기본값)` when enrichment was attempted and failed,
  naming a usage limit specifically. This is the only surface the owner sees on a phone. It stays
  quiet when enrichment succeeds, and when `CLAUDE_ENABLED=false` (a deliberate choice, not a
  fault — no need to nag on every note).

Tests: `test_claude_cli.py` drives the **real subprocess path** against a fake `claude` script on
disk (argv, stdin delivery, resume, exit codes, timeout+kill, missing binary, off-PATH resolution)
— no model runs. `test_parsing.py`, `test_prompts.py`, the enrichment/fallback cases in
`test_text_handler.py`, and the `claude-engine` probe cases in `test_health.py` cover the rest.
Suite: 88 → 165.

## Human-in-the-loop via session preservation

`core/session_store.py` + `handlers/conversation.py` implement a per-review state machine:

```
IDLE ──job needs review──▶ AWAITING_REVIEW ──owner reply (bot DM)──▶ FINALIZING ──▶ IDLE
                                  │
                                  └── "취소" / timeout ──▶ IDLE
```

- Capture (Saved Messages) and review (bot DM) are separate channels, so the review reply is **not**
  mistaken for a new capture and cannot loop.
- When a job produces a draft + review questions, persist
  `{claude_session_id, temp_paths, draft_path, created_at}` (in-memory + on-disk JSON under
  `state/` for crash recovery) and send the review block to the **bot DM**. The reply bot enables
  `getUpdates` polling (filtered to the owner id) while `AWAITING_REVIEW`, and disables it when the
  review ends — polling is only ever active briefly, so the 24h Bot-API limit is irrelevant.
- The owner's bot-DM reply resumes the same Claude session so corrections, glossary updates, and
  finalization happen with full context. `확인`/`ok` accepts the draft as-is; `취소` aborts.
- This is what makes multi-turn review work without re-sending context each turn, and is the key
  reason the engine is Claude Code sessions rather than one-shot API calls.

## A. Audio → meeting note (reuse `meeting-transcriber`)

1. Download the audio to `.tmp/` via `message.download_media(...)` (Telethon); run local
   `mlx-whisper` `whisper-large-v3-turbo` (port of
   `~/Projects/meeting-transcriber/.claude/skills/meeting/scripts/transcribe.py`) → transcript.
2. Drive note generation with `claude -p`, pointing it (via `--add-dir` + prompt template) at the
   **shared glossary**
   `~/Projects/meeting-transcriber/.claude/skills/meeting/glossary.md` and the `/meeting` skill's
   template/steps. Claude applies glossary substitutions, drafts the note, and returns the review
   block (flagged terms + timestamps).
3. Relay the review block; on the user's reply, resume the session to apply corrections, **append
   new glossary entries**, and save the note to `0_inbox`.
4. Delete the original audio and temp files on success.

## B. Image → described note (VLM via `claude -p`)

- Save the original image into the vault (`.assets/` or note-adjacent). Call `claude -p` with the
  image path for a detailed description + OCR. Write a note **embedding the original image**
  (relative link) plus the description/OCR to `0_inbox`. (No review loop unless the user wants
  one.)

## C. Document files → Markdown

- `converters/doc_to_markdown.py` does deterministic base extraction, optionally refined by a
  `claude -p` structuring/summarizing pass (the `docx`/`pdf`/`pptx` Anthropic skills can be
  invoked here). Save the `.md` to `0_inbox`; **move the original to `~/Downloads/`** via
  `files/originals.py`.

## D. Markdown passthrough

- A sent `.md` file is saved as-is directly to `0_inbox` (extension check in
  `document_handler.py` before the converter path; no conversion, no LLM).

## E. Text notes (optional upgrade)

- Optionally call `claude -p` for title/tags/summary in frontmatter, keeping the Phase 1 no-LLM
  path as fallback for offline use.

## Original-file policy (`files/originals.py`)

Audio → delete after success · Image → keep (embedded) · pdf/pptx/docx/… → move to `~/Downloads/`
· Markdown → save to inbox (it *is* the note).

## Open decisions

1. ~~**File-writing responsibility**~~ — **resolved (increment 1): the bot writes.** Claude returns
   text only; the engine grants no write access, so no permissive `--permission-mode` is needed and
   every pipeline stays unit-testable. Revisit only if a pipeline genuinely needs Claude to edit
   files in place.
4. ~~**Claude `--model` and per-job timeout budgets**~~ — **deferred cheaply (increment 1): both are
   env-tunable** (`CLAUDE_MODEL`, `CLAUDE_TIMEOUT_SEC`), defaulting to `sonnet` / 120s. Per-job
   overrides exist (text enrichment uses 60s). Audio/document jobs can raise their own budget when
   built, so no up-front decision is needed.

Still open (each is confirmed when its increment starts):

2. Whether meeting notes should also trigger the glossary **git commit/push** the `/meeting` skill
   does (both the vault and meeting-transcriber are git repos). *(increment 5)*
3. Base document converter: `markitdown` vs `pandoc`-based vs Claude-skill-driven. *(increment 2)*
5. Whether to physically **share** the meeting-transcriber glossary file or copy/symlink it.
   *(increment 5)*

### Cost note

A `claude -p` call carries ~9–14k cache-creation tokens (agent system prompt + tool definitions)
regardless of prompt size — roughly $0.03–0.09 per cold call on `sonnet`. That is negligible for a
personal capture bot at a few notes a day, but it is why `CLAUDE_ENABLED=false` exists and why
enrichment is optional rather than load-bearing. If it ever matters, `CLAUDE_MODEL=haiku` is the
first lever.

## Testing strategy

Mock `engine/claude_cli.py` and the STT step so the conversation state machine, session
resume/finalize, glossary update, original-file moves, and file output are all testable without
real model runs, model downloads, or network. Add fixtures for pending-session persistence and
timeout/`취소` handling.
