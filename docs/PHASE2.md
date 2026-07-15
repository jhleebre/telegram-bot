# Phase 2 — Development Plan (increments 1–2 of 5 shipped)

Phase 2 adds the LLM/VLM-powered pipelines on top of the Phase 1 skeleton. The engine is
**Claude Code in headless mode** (`claude -p`), and multi-turn human-in-the-loop review is built
on **Claude Code resumable sessions** (`--session-id` / `--resume`). The routing table and stub
handlers from Phase 1 mean Phase 2 mostly fills in handler bodies and adds a few new modules.

> **Picking this up in a fresh session?** Read, in order: this header → *Delivery approach* →
> *Increment 1 (as built)* (the engine's contract and the three real-world bugs it hit) →
> *Increment 2 (as built)* (the file pipelines and the isolation the PDF route depends on) →
> *Usage-limit policy* (binding on every later increment) → *Next up: increment 3*.
> Suite: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest` — **263 passing**, no network or model runs.

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
- [x] **2. PDF → Markdown** (`document_handler` + `files/originals.py`) — no review loop, no
      converter, no new dependency; simplest file pipeline. Includes `.md` passthrough.
      **Shipped** — see "Increment 2 (as built)" below.
- [ ] **3. Image → described note** (`image_handler`, VLM via `claude -p`) — still no review loop.
- [ ] **4. Human-in-the-loop plumbing** (`core/session_store.py`, `handlers/conversation.py`, bot-DM
      polling) — build and test the review state machine on a simple case first.
- [ ] **5. Audio → meeting note** (`audio_handler`, `mlx-whisper`, glossary, review) — the most
      complex; depends on 1 and 4.

Rationale: features 2–3 are one-shot and low-risk, so they validate the engine (1) before the
harder two-way review flow (4) and the STT-heavy audio pipeline (5) are attempted.

### Increment 2 (as built) — PDF → Markdown

Delivered: `handlers/document_handler.py` (five routes), `files/originals.py`, `files/text_files.py`,
`engine/prompts/pdf_to_markdown.md`, the `DOWNLOADS_DIR` setting, `ClaudeCLI.run(cwd=…)`, and —
after owner verification (below) — `ClaudeCLI.check_auth` behind an upgraded `claude-engine` health
probe. Suite: 171 → **263**.

**The decisions recorded below were all made before building, and every one of them held.** They
are kept as the *why*; the "As built" subsection at the end of this section is the *what*.

Scope was: `handlers/document_handler.py` (was a stub) + `files/originals.py`. No review loop, no
converter module, no new dependency. The **`.md` passthrough (item D) was built first** — no LLM, a
few lines, and it proved the routing end-to-end before any PDF work.

**Decision 3 is resolved: PDF only. Claude Code reads the PDF natively; the owner exports to PDF
from MS Office and sends that.** Everything below was probed against the real CLI on the same
document, so it is measured, not assumed.

Three candidates were tried before landing here:

| Route | Turns | Est. | Tool denials | Permission mode | Verdict |
|-------|-------|------|--------------|-----------------|---------|
| `.docx`, increment 1's hermetic flags | 26 | — | 13 | default | ❌ **fabricated** (below) |
| `.docx` + shell | 8 | ~$0.10 | 13 | **`bypassPermissions`** | works, but arms the model |
| **`.pdf` direct** | **3** | **~$0.041** | **0** | **none** | ✅ **adopted** |

Why PDF-only wins outright:

- **No shell, so no injection surface.** `bypassPermissions` hands Claude a shell while document
  content is a prompt-injection vector — only the owner can *write* to Saved Messages, but that does
  not make a forwarded PDF owner-*authored*. The PDF path needs no tools beyond `Read`, so
  increment 1's hermetic flags stay exactly as they are.
- **Cheapest LLM path in the project**: 3 turns / ~$0.041 — less than a text memo's enrichment — and
  the output was *better* structured than the shell route's (it inferred `##` headings).
- **No dependency and no conversion step.** An earlier plan converted office → PDF with LibreOffice
  (~700MB). Dropping non-PDF formats removes that, and removes `converters/doc_to_markdown.py`
  entirely. (For the record: nothing on this machine converts office files today — `soffice`,
  `libreoffice`, `pandoc`, `unoconv` all absent, `cupsfilter` returns 0 bytes. The zero-install
  `textutil` → HTML → Chrome → PDF route works for `.docx` but is pointless: fidelity is capped at
  the `textutil` step, so the layout fidelity that motivated PDF is already gone. `pptx`/`xlsx` had
  no path at all. Manual export from MS Office beats all of it — and it is the *only* option that
  renders with full fidelity.)

**Two findings that bind the implementation regardless of route:**

1. **It fabricates rather than failing, and reports success.** Blocked from a `.docx`, the model
   read a *neighbouring* `.rtf` that happened to hold the same content, produced convincing
   Markdown, and returned `is_error: False`. In production that is a note built from the wrong
   source, saved silently. So: **stage the PDF alone in an isolated temp dir** (`--add-dir` that dir
   and nothing else), and **never treat `is_error: False` as proof of provenance** — have the prompt
   emit a sentinel (e.g. `CONVERSION_FAILED`) and check for it before writing a note.
2. **`Read` pages PDFs**: ≤20 pages per request, and an explicit range is required above 10 pages.
   A long deck is therefore several turns — budget the timeout above the text path's 60s cap.

Constraints inherited from increment 1 (do not re-litigate):

- **The bot writes files**, Claude returns text only (decision 1, resolved).
- **`DeferMessage` before any side effect.** Do not move the original to `~/Downloads/` or write the
  note until after the last `claude -p` call — a deferred message replays the handler from scratch,
  so an early side effect gets duplicated. Route the original through `files/originals.py` **last**.
- **Degrade on non-limit failure** as text does — but note there is no no-LLM fallback here, so
  "degrade" likely means *don't write a note and say why*, not *write a worse one*.

#### The remaining two, decided

**Unsupported office formats (`docx`/`pptx`/`xlsx`/…) → reply and treat as processed.**
`document_handler` returns a normal `HandlerResult(reply="… PDF로 내보내서 보내주세요", saved_path=None)`,
so `_process` advances the HWM exactly as it does for `handle_text`'s empty-message reply. **No new
plumbing.** The two alternatives are both wrong here:

- Not `DeferMessage` — that is for conditions time will fix. A `.docx` never becomes supported, so
  the bot would halt on every Start forever.
- Not an exception — the skip path fires a `⚠️ 메시지 처리 실패` DM, which reads like a fault. This
  isn't one; the bot did exactly what it should and is telling the owner what to do.

Marking it processed is what stops the same `.docx` re-nagging on every subsequent Start.

**`.txt` / `.csv` → converted to Markdown, not passed through.** MarkNotes only surfaces `.md`, so a
passthrough file is invisible in the vault — saving it would look like success and produce nothing
the owner can find. These are **text-like, not PDF-like**: route them to a text path, not the PDF
one. There is no page to render, so no PDF, no `Read`, no tools.

- `.txt` → the content *is* the body. Reuse `handle_text`'s enrichment for title/tags/summary.
- `.csv` → render the Markdown table **deterministically** (stdlib `csv`), then take the same note
  path. **Do not let the LLM transcribe the table.** It would retype the owner's numbers, and a
  silently altered figure in a data file is precisely the error nothing downstream can catch. The
  LLM's job stays metadata-only — the same boundary the text path already holds (body is always the
  original; the model only supplies frontmatter).

Two landmines for whoever builds this:

- **Encoding.** Excel on Windows writes Korean CSV as CP949/EUC-KR, not UTF-8. A naive
  `read_text()` either raises or mojibakes. Detect/fallback rather than assuming UTF-8 — and treat a
  decode failure as a *permanent* failure (reply and move on), never a `DeferMessage`.
- **Size.** A large `.csv` should not become a giant note. Cap it, and say so in the reply rather
  than writing something unusable.

#### As built

`handle_document` dispatches on **extension**, into five routes:

| Route | Body comes from | LLM | Original |
|-------|-----------------|-----|----------|
| `.md` | the file, byte-for-byte | never called | none — it *is* the note |
| `.txt` | the file's text | metadata only | → `~/Downloads/` |
| `.csv` | `files/text_files.render_csv_table` (stdlib `csv`) | metadata only | → `~/Downloads/` |
| `.pdf` | **Claude** (`Read`s the PDF natively) | body + title | → `~/Downloads/` |
| else | — | never called | never even downloaded |

Points worth knowing before touching this:

- **Isolation is `cwd` + `--add-dir`, not `--add-dir` alone.** `ClaudeCLI.run` gained a `cwd`
  parameter for this. Without it the subprocess inherits the *app's* working directory — the
  project tree — which is exactly the pile of neighbouring files the model was observed to
  fabricate from. The PDF is staged alone in a `TemporaryDirectory` that is both the job's `cwd`
  and its only `--add-dir`. `test_pdf_is_staged_alone_in_an_isolated_dir` asserts the directory
  listing *as the model would see it*, at call time.
- **No new CLI flags.** No `--permission-mode`, no `--allowedTools`: the PDF route needs only
  `Read`, which the default mode allows inside `cwd`/`--add-dir`. Increment 1's hermetic flags are
  untouched — the note in *Increment 1 (as built)* predicting otherwise was written before the
  PDF-only decision and no longer applies.
- **The sentinel is checked on the first line, not by substring.** A document that legitimately
  contains the word `CONVERSION_FAILED` (an error-code table, say) must still convert; a model that
  leads with `CONVERSION_FAILED`, `` `CONVERSION_FAILED` ``, `# CONVERSION_FAILED`, or
  `CONVERSION_FAILED: <excuse>` must not. Empty output counts as failure too.
- **`enrich_or_fallback` is now the shared enrichment entry point** in `text_handler`, used by both
  `handle_text` and the `.txt`/`.csv` route. It raises `DeferMessage` on a usage limit and swallows
  everything else into a `degraded` reason, which is what keeps the "call it before any side
  effect" rule enforceable in one place.
- **`.md` filenames are normalized** to the vault's `YYMMDD-HHMM-<slug>.md`, so a sent note sorts
  into the inbox with the rest. Only the *name* changes — the content is written byte-for-byte,
  frontmatter and all. (`.markdown` becomes `.md`.)
- **Caps** (all in `document_handler`): 2MB per file, 200 CSV rows, 50,000 characters of `.txt`.
  Truncation is reported in the reply *and* in the note body, because the full file is in
  `~/Downloads` — a capped note is a preview, not a loss.
- **Encoding order is `utf-8-sig` → `cp949`, plus UTF-16 when a BOM says so.** `latin-1` is
  deliberately absent as a last resort: it decodes any byte sequence, which would convert a
  failure we report into mojibake we do not.
- **Degradation, per route** (settling the "decide when each increment is built" row): `.txt`/`.csv`
  degrade exactly like text (the body is already the note; only frontmatter is weaker). **`.pdf`
  has no fallback** — rendering the page *is* the LLM's job — so a non-limit failure writes **no
  note**, replies with the reason, and still files the original to `~/Downloads`. Same for the
  sentinel, and for `CLAUDE_ENABLED=false`.
- **A decode failure or an oversized file replies and advances** (permanent, per the usage-limit
  policy's table) rather than raising — but the original is still moved, so the owner gets the file
  back either way.

**Verification status.** Everything except the model's actual conversion was driven for real: the
`.md` / `.txt` / `.csv` / unsupported routes end-to-end with real files (including a CP949 CRLF CSV
written the way Excel writes one), and the PDF route end-to-end against the real `claude` binary —
which in a *nested* Claude Code session cannot authenticate (`Not logged in · Please run /login`,
`is_error: true`, `api_error_status: null`). That accidentally proved the non-limit degradation
path against the real CLI: no note, reason in the reply, original filed. It also means **the
conversion quality itself is owner-verified only** — a fresh session cannot probe it from inside
Claude Code.

**Owner verification, round 1 (the login gap).** The owner's first real PDF failed with
`Not logged in · Please run /login` — and a plain text memo then failed the same way, proving the
CLI's login had simply expired (this machine, not the pipeline). The code behaved correctly (no
fabricated note, original filed, reason relayed, no halt — a login failure is not `DeferMessage`
territory). But the `claude-engine` health probe had been green throughout, because it only checked
that the binary was *findable*. Closed here: the probe now also runs `claude auth status --json` and
reports **DEGRADED — logged out**. Re-login (`claude`, then `/login`) is an owner action; PDF
conversion quality remains to be verified once logged back in.

## Scope

| Input | Output | Original file |
|-------|--------|---------------|
| Audio (m4a/wav/mp3/voice) | Meeting note (glossary-corrected, reviewed) → `0_inbox` | deleted after success |
| Image (screen capture) | Description + OCR note, original embedded → `0_inbox` | kept (embedded/`.assets`) |
| **PDF** | ✅ **shipped** — Markdown (Claude reads the PDF natively) → `0_inbox` | **moved to `~/Downloads/`** |
| docx / pptx / xlsx / … | ✅ **shipped** — **out of scope by decision**: reply asking for a PDF export | — |
| txt / csv | ✅ **shipped** — Markdown note (csv → table, rendered deterministically) → `0_inbox` | **moved to `~/Downloads/`** |
| Markdown (`.md`) | ✅ **shipped** — saved as-is → `0_inbox` | is the note |
| Text (upgrade) | ✅ **shipped** — LLM-enriched title/tags/summary (fallback = Phase 1 path) | — |

## New modules

```
src/contextbot/
├── engine/                 # ✅ built in increment 1
│   ├── claude_cli.py       # wrapper over `claude -p` (async subprocess, JSON parse, resolution)
│   ├── parsing.py          # recover a JSON object from a model's free-text reply
│   └── prompts/            # templates: text_enrich ✅ / pdf_to_markdown ✅ / meeting / image
├── core/
│   └── session_store.py    # per-chat conversation state + Claude session_id persistence
├── handlers/
│   ├── audio_handler.py    # (fill in) audio → meeting note
│   ├── image_handler.py    # (fill in) image → described note
│   ├── document_handler.py # ✅ built in increment 2 — md / txt / csv / pdf / unsupported
│   └── conversation.py     # routes a plain-text reply into an active pending session
└── files/                  # ✅ built in increment 2
    ├── originals.py        # original-file policy (Downloads / delete / embed)
    └── text_files.py       # encoding detection + deterministic CSV → Markdown table
```

Additional runtime deps: `mlx-whisper` (STT, Apple Silicon) — **and nothing else**. Documents need
no converter library and no LibreOffice: the scope is PDF-only and Claude Code reads PDFs natively
(see decision 3). `ffmpeg` is already present. `claude` CLI is already installed (v2.1.187 verified).

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
  **Amended by increment 2 (as built):** an intermediate draft of this bullet predicted that
  reading a document would force `ClaudeCLI.run` to expose `--permission-mode` / `--allowedTools`.
  It did not — that was true only of the abandoned shell-driven `.docx` route. The default mode
  allows `Read` inside `cwd`/`--add-dir`, so **no permission mode and no new flags are used**, and
  the hermetic flags are unchanged. What increment 2 *did* need was `run(cwd=…)`, so the job runs
  in the isolated staging dir. "The bot writes the note" stands; "Claude needs no tools beyond
  `Read`" stands.
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
| Document (2) | ✅ **decided when built, and it splits.** `.txt`/`.csv` degrade like text (the body is the file, so only frontmatter weakens). `.pdf` has **no fallback** — the LLM *is* the converter — so: no note, reply with the reason, original still filed to `~/Downloads` |
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
  and — since increment 2 — that it is *logged in*: the probe runs `claude auth status --json` (a
  local, no-usage call) and reports **DEGRADED — logged out** for a resolvable-but-signed-out CLI.
  A **usage limit is still invisible to it**, because distinguishing that would cost a real job.
- The **bot DM reply** appends `⚠️ …(제목/태그는 기본값)` when a *non-limit* enrichment failure
  degraded the note. This is the only surface the owner sees on a phone. It stays quiet when
  enrichment succeeds, and when `CLAUDE_ENABLED=false` (a deliberate choice, not a fault — no need
  to nag on every note). A usage limit no longer degrades: it defers (see the usage-limit policy).

Tests: `test_claude_cli.py` drives the **real subprocess path** against a fake `claude` script on
disk (argv, stdin delivery, resume, exit codes, timeout+kill, missing binary, off-PATH resolution)
— no model runs. `test_parsing.py`, `test_prompts.py`, the enrichment/fallback cases in
`test_text_handler.py`, and the `claude-engine` probe cases in `test_health.py` cover the rest.
Suite: 88 → 171.

Increment 2 adds `test_document_handler.py` (all five routes, the sentinel variants, staging
isolation, deferral-leaves-no-side-effect for both `.pdf` and `.txt`), `test_text_files.py`
(CP949/UTF-16 decoding, CSV escaping and caps), and `test_originals.py`. The `check_auth` login
probe (added during owner verification) is covered in `test_claude_cli.py` and `test_health.py`.
Suite: 171 → 263.

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

### Next up: increment 3 — image → described note

Scope: `handlers/image_handler.py` (still a stub) + the `image` prompt template. Still no review
loop. Inherited from increment 2 and not up for re-litigation:

- **`files/originals.py` is where the original-file policy lives.** Images are *kept* (embedded),
  not moved — add the policy there rather than in the handler.
- **`ClaudeCLI.run(cwd=…, add_dirs=[…])` is how a file reaches the model.** An image is read from
  disk exactly as a PDF is, so stage it the same way: alone, in a temp dir that is both `cwd` and
  the only `--add-dir`. The fabrication risk is identical.
- **Side effects after the last LLM call**, and a usage limit raises `DeferMessage` first.
- Item B below already asks the open question: with no fallback (a described note *is* the LLM
  output), does a non-limit failure save the image with a stub note, or nothing at all? Increment 2
  answered the same question for PDF with *nothing at all, and say why* — the image case differs in
  that the original is worth keeping regardless.

## C. PDF → Markdown

- **PDF only.** Stage the downloaded PDF alone in a temp dir, `--add-dir` that dir, and let Claude
  Code's native PDF `Read` produce the Markdown — no converter, no shell, no permission mode.
  Save the `.md` to `0_inbox`; **move the original PDF to `~/Downloads/`** via `files/originals.py`,
  after the LLM call (see the usage-limit policy).
- **Other office formats are out of scope by decision**: the owner exports to PDF from MS Office and
  sends that. `document_handler` should reply with that instruction rather than a generic stub, so
  the bot never silently does nothing with a `.docx`.

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
3. ~~Base document converter~~ — **resolved: PDF only, read natively by Claude Code.** No library
   (owner's call — library output is mediocre on layout), no shell-armed agent, and no LibreOffice:
   the owner exports to PDF from MS Office. Probed: 3 turns / ~$0.041 / zero tool denials / no
   permission mode. See *Next up: increment 2*. *(resolved)*

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
