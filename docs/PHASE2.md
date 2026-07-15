# Phase 2 — Development Plan (increment 1 of 5 shipped)

Phase 2 adds the LLM/VLM-powered pipelines on top of the Phase 1 skeleton. The engine is
**Claude Code in headless mode** (`claude -p`), and multi-turn human-in-the-loop review is built
on **Claude Code resumable sessions** (`--session-id` / `--resume`). The routing table and stub
handlers from Phase 1 mean Phase 2 mostly fills in handler bodies and adds a few new modules.

> **Picking this up in a fresh session?** Read, in order: this header → *Delivery approach* →
> *Increment 1 (as built)* (the engine's contract and the three real-world bugs it hit) →
> *Usage-limit policy* (binding on every later increment) → *Next up: increment 2*.
> Suite: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest` — **171 passing**, no network or model runs.

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

### Next up: increment 2 — document files → Markdown

Scope: `handlers/document_handler.py` (currently a stub), `converters/doc_to_markdown.py`,
`files/originals.py`. No review loop. Includes the `.md` passthrough (item D), which needs no
converter and no LLM — **build that first**, it is a few lines and proves the routing end-to-end.

**Open decision 3 is resolved: Claude Code drives the conversion, not a Python library** (owner's
call — library output is mediocre on layout, tables, and structure) — **and it reads a PDF, not the
original office file.** The final shape is in *The shape: normalize to PDF* below; read this section
first only for the two findings that constrain it, both probed against the real CLI on a `.docx`:

| Flags | Turns | Result |
|-------|-------|--------|
| Increment 1's hermetic flags (default permission mode) | 26 | ❌ **Fabricated** — see below |
| `--permission-mode bypassPermissions` + isolated dir | 8 | ✅ accurate (67s, ~$0.10 est) |

*(Neither is the plan — normalizing to PDF beats both. But the fabrication finding applies to **any**
route, and the denial finding is why "just point Claude at the .docx" is not an option.)*

- **Default permission mode denies Bash**, and `Read` cannot parse a `.docx` (a ZIP). All 13 Bash
  attempts (`unzip`, `python3`, `node`, `perl`, `strings`, `textutil`, …) were denied, so it had no
  way to open the file at all. A permission mode is therefore **required** — the one thing
  increment 1 concluded was unnecessary, because the engine was text-in/text-out. Documents are not.
- **It fabricates rather than failing, and reports success.** Blocked from the `.docx`, it read a
  *neighbouring* `.rtf` that happened to hold the same content, produced convincing Markdown, and
  returned `is_error: False`. In production that is a note built from the wrong source, saved
  silently. Two consequences: **stage the file alone in an isolated temp dir** (`--add-dir` that
  dir, nothing else), and **do not treat `is_error: False` as proof the output came from the
  document** — have the prompt emit a sentinel on failure and check for it.
- **Cost**: ~8 turns / ~67s / ~$0.10 est per document vs 1 turn / ~4s / ~$0.05 for a text memo — it
  burns the plan's usage allowance roughly twice as fast, and is slow enough that the per-job
  timeout needs raising above the 60s the text path uses.

#### The shape: normalize to PDF, then one PDF → Markdown path (owner's call, probed)

`bypassPermissions` gives Claude a shell, and **document content is a prompt-injection vector**:
only the owner can write to Saved Messages, but that does not make the *document* theirs — a
forwarded PDF can carry "ignore previous instructions and …" straight into a model holding a shell.

**Converting `docx`/`pptx` → PDF first removes the shell entirely**, because Claude Code's `Read`
parses PDFs natively. Probed against the real CLI, same document, isolated dir:

| Input | Turns | Est. | Tool denials | Permission mode |
|-------|-------|------|--------------|-----------------|
| `.docx` with a shell | 8 | ~$0.10 | 13 | **`bypassPermissions` required** |
| **`.pdf` direct** | **3** | **~$0.041** | **0** | **none needed** |

The PDF path is better on every axis — no shell, no permission mode, ~⅓ the turns, ~½ the plan-usage
burn, and the output was *better* structured (it inferred `##` headings). It also keeps increment 1's
hermetic flags intact. **Adopt it: normalize every office format to PDF, then keep exactly one
LLM-facing path (PDF → Markdown).** Conversion is deterministic — no LLM, so nothing to fabricate.

**The cost: LibreOffice becomes a real dependency.** Nothing on this machine converts these today
(`soffice`, `libreoffice`, `pandoc`, `unoconv` all absent; `cupsfilter` produced 0 bytes on both
`.docx` and HTML):

| Input | Zero-install path | Verdict |
|-------|-------------------|---------|
| `pdf` | passthrough | ✅ best case |
| `docx` | `textutil` → html → Chrome headless → pdf (both present, works) | ⚠️ **pointless** — fidelity is capped at the `textutil` step, so the layout benefit that motivated PDF is already gone. Library quality with extra steps. |
| `pptx` / `xlsx` | none — `textutil` supports only `txt, rtf, rtfd, html, doc, docx, wordml, odt, webarchive` | ❌ no path at all |

So real layout fidelity needs a real renderer: **`brew install --cask libreoffice`** (~700MB), then
`soffice --headless --convert-to pdf`. That is the price of this design. It looks like a good trade:
one install, once, versus handing a shell to a model on every document — and it unlocks pptx/xlsx,
which have no path otherwise.

**Note for whoever builds this:** `soffice` lives at
`/Applications/LibreOffice.app/Contents/MacOS/soffice` — **not on PATH even in a terminal**, let
alone under the Dock's minimal PATH. Resolve it with the `engine.claude_cli.resolve_executable`
pattern; a bare `which soffice` will fail on a machine where LibreOffice is installed and working.

Still to decide in increment 2: what to do when LibreOffice is absent (degrade to the
textutil/Chrome path for docx? reply "지원하지 않음"? make it a health probe like `claude-engine`?),
and whether long PDFs need paging (`Read` takes ≤20 pages per request and requires an explicit
range above 10 pages — a 60-page deck is several turns).

Constraints inherited from increment 1 (do not re-litigate):

- **The bot writes files**, Claude returns text only (open decision 1, resolved).
- **`DeferMessage` before any side effect.** `handle_document` must not move the original to
  `~/Downloads/` or write the note until after the last `claude -p` call — a deferred message
  replays the handler from scratch, so an early side effect gets duplicated. Route the original
  through `files/originals.py` **last**.
- **Degrade on non-limit failure** like text does: the deterministic converter output is a usable
  note on its own, so save it un-refined rather than losing the document.
- Resolve any new binary (`pandoc`, …) with `engine.claude_cli.resolve_executable`-style fallback,
  **never a bare PATH lookup** — a Dock-launched app has a minimal PATH (see below).

## Scope

| Input | Output | Original file |
|-------|--------|---------------|
| Audio (m4a/wav/mp3/voice) | Meeting note (glossary-corrected, reviewed) → `0_inbox` | deleted after success |
| Image (screen capture) | Description + OCR note, original embedded → `0_inbox` | kept (embedded/`.assets`) |
| pdf / pptx / docx / xlsx / … | Converted Markdown → `0_inbox` | **moved to `~/Downloads/`** |
| Markdown (`.md`) | Saved as-is → `0_inbox` | is the note |
| Text (upgrade) | ✅ **shipped** — LLM-enriched title/tags/summary (fallback = Phase 1 path) | — |

## New modules

```
src/contextbot/
├── engine/                 # ✅ built in increment 1
│   ├── claude_cli.py       # wrapper over `claude -p` (async subprocess, JSON parse, resolution)
│   ├── parsing.py          # recover a JSON object from a model's free-text reply
│   └── prompts/            # prompt templates: text_enrich ✅ / meeting / image / doc-convert
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

Additional runtime deps: `mlx-whisper` (STT, Apple Silicon); **LibreOffice** for office → PDF
(`brew install --cask libreoffice`; not installed yet — verified absent, along with `pandoc` /
`unoconv`, and `cupsfilter` fails). `ffmpeg` is already present. `claude` CLI is already installed
(v2.1.187 verified).

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
  **Superseded for increment 2 onward:** that holds only while the input *is* the prompt. A document
  must be read from disk, and with the default permission mode every Bash attempt is denied — so
  `ClaudeCLI.run` will need to expose `--permission-mode` / `--allowedTools`. "The bot writes the
  note" still stands; "Claude needs no tools" does not. See *Next up: increment 2*.
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

### Usage-limit policy — defer and replay (decided; implemented)

**When the usage limit is exhausted, the bot stops and leaves the message unprocessed. The owner
restarts after the limit resets, and catch-up replays it from where it left off.**

This needs no queue: the HWM already advances only on success, Saved Messages is durable, and
catch-up already replays from the HWM. **Deferring is lossless** — which is the same property that
made Saved Messages the input in Phase 1.

**Transient vs permanent is the whole distinction:**

| Failure | Retry later? | Policy |
|---------|--------------|--------|
| **Usage limit** (`ClaudeUsageLimit`) | succeeds after reset | **Defer + halt.** HWM untouched; STOPPED with the reason; DM explains. |
| Missing CLI, timeout, non-JSON | may keep failing; not time-bound | Degrade (text keeps the Phase 1 path) — halting here would stop the bot on every Start and wedge it |
| Corrupt file, a bug in our code | fails identically forever | **Skip + announce.** Advance the HWM deliberately and DM `id=N … 건너뜁니다`. Halting would wedge the bot; silence would lose the message. |

`handlers/base.DeferMessage` is the handler→client contract (a domain signal, so `core` stays
decoupled from the engine). A handler must raise it **before any side effect**, because the replay
re-runs the handler from scratch. Applies to text too: a limit defers rather than saving a weaker
note the owner would have to find and fix later.

#### Three bugs this policy surfaced (all pre-existing, all fixed)

1. **Stranding — the blocker.** `_process` swallowed every exception and `catch_up` kept going. The
   HWM is a *single* watermark, so a later success advanced it **past** the failed message, which
   the `id <= hwm` guard then skipped forever. Proven before fixing: backlog `[1,2,3]` with 2
   failing → run 1 processed all three, HWM = 3, restart processed **nothing**. Message 2 was gone.
   This lost messages in Phase 1 already, independent of Phase 2. Catch-up now breaks at the first
   deferral, which is what keeps the replay correct and ordered.
2. **`start()` set RUNNING unconditionally** after catch-up, so a halt would have been overwritten.
3. **The UI followed only ERROR**, not STOPPED, so a self-initiated stop left the button on "Stop"
   with `_running=True` — the owner would have to dead-click Stop before Start worked.

#### Degradation per pipeline (decide when each increment is built)

The deferral policy covers *limits*. Each pipeline still needs an answer for a **non-limit** engine
failure, and text is the only one with a real no-LLM path:

| Pipeline | If the engine fails for a non-limit reason |
|----------|-------------------------------------------|
| Text (1) | ✅ Phase 1 path; note keeps full fidelity, only metadata is weaker |
| Document (2) | Deterministic converter output can still be saved un-refined — degrade like text |
| Image (3) | No fallback: a described note *is* the LLM output. Save the image + a stub note? |
| Audio (5) | **No fallback.** STT is local, so the transcript survives — but the meeting note does not. Saving the raw transcript beats losing the recording. |
| Review (4) | A failure mid-review would strand a session in `AWAITING_REVIEW`. The state machine must handle "cannot resume right now" without dropping the draft. |

**Replay cost (increment 5).** A deferred message re-runs its handler *from the start* — for audio
that means re-downloading and re-running Whisper (minutes). Either cache the transcript keyed by
message id, or accept the rework. Non-negotiable either way: **all side effects (moving the
original to `~/Downloads`, deleting the audio, writing the note) must come after the last LLM call**,
or be idempotent — otherwise the replay duplicates them.

A **circuit breaker** (skip the engine for N minutes after a `ClaudeUsageLimit`) is now moot for
capture: the bot halts on the first limit rather than retrying per message.

#### Making degradation visible

The fallback is deliberately silent in the note (that is the point — never lose a capture), which
also made a *broken engine* indistinguishable from a working one without reading the log. Two
surfaces now report it:

- The health panel carries a **`claude-engine`** probe showing the resolved path + model; a missing
  CLI reports **DEGRADED** (not ERROR — capture still works). It proves the binary is *findable*,
  not that it can *run* — a usage limit is invisible to it.
- The **bot DM reply** appends `⚠️ …(제목/태그는 기본값)` when a *non-limit* enrichment failure
  degraded the note. This is the only surface the owner sees on a phone. It stays quiet when
  enrichment succeeds, and when `CLAUDE_ENABLED=false` (a deliberate choice, not a fault — no need
  to nag on every note). A usage limit no longer degrades: it defers (see the usage-limit policy).

Tests: `test_claude_cli.py` drives the **real subprocess path** against a fake `claude` script on
disk (argv, stdin delivery, resume, exit codes, timeout+kill, missing binary, off-PATH resolution)
— no model runs. `test_parsing.py`, `test_prompts.py`, the enrichment/fallback cases in
`test_text_handler.py`, and the `claude-engine` probe cases in `test_health.py` cover the rest.
Suite: 88 → 171.

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
3. ~~Base document converter~~ — **resolved: normalize to PDF, then Claude reads the PDF.**
   Not a Python library (owner's call — library output is mediocre on layout), and not a shell-armed
   agent either: `docx`/`pptx` → PDF deterministically, then Claude Code's native PDF `Read`. Probed:
   3 turns / ~$0.041 / zero tool denials / no permission mode, vs 8 turns / ~$0.10 /
   `bypassPermissions` for the shell route. Adds a **LibreOffice** dependency (~700MB) — that is the
   trade. See *Next up: increment 2*.
5. Whether to physically **share** the meeting-transcriber glossary file or copy/symlink it.
   *(increment 5)*

### Cost note — the budget is plan usage, not dollars

A `claude -p` call carries ~9–14k cache-creation tokens (agent system prompt + tool definitions)
regardless of prompt size. The result JSON reports that as `total_cost_usd` (~$0.03–0.09 per cold
call on `sonnet`), and the engine logs it as `~$… est`.

**That figure is not a bill.** The CLI computes it locally from token counts at standard API rates.
The owner runs Claude Code on a **Pro subscription with usage credits off**, so per the Claude Code
docs, "Max and Pro subscribers have usage included in their subscription, so the session cost figure
isn't relevant for billing purposes." Nothing is charged beyond the subscription — usage credits are
strictly opt-in, and with them off, hitting the limit simply blocks until the window resets (which
is the `ClaudeUsageLimit` path above).

What the number *does* measure is **how fast the bot consumes the plan's usage allowance** — the
5-hour and weekly windows, which are **shared with claude.ai chats and the owner's own Claude Code
sessions** ("your work in the terminal and your chats draw from one pool"). So the real cost of
enrichment is that a memo competes with the owner's development work for the same pool. That is why
`sonnet` (not `opus`) is the default, why `CLAUDE_ENABLED=false` exists, and why enrichment is
optional rather than load-bearing. If the allowance ever feels tight, `CLAUDE_MODEL=haiku` is the
first lever.

## Testing strategy

Mock `engine/claude_cli.py` and the STT step so the conversation state machine, session
resume/finalize, glossary update, original-file moves, and file output are all testable without
real model runs, model downloads, or network. Add fixtures for pending-session persistence and
timeout/`취소` handling.
