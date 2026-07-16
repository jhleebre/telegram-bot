# Phase 2 — Development Plan (increments 1–4 of 5 shipped)

Phase 2 adds the LLM/VLM-powered pipelines on top of the Phase 1 skeleton. The engine is
**Claude Code in headless mode** (`claude -p`), and multi-turn human-in-the-loop review is built
on **Claude Code resumable sessions** (`--session-id` / `--resume`). The routing table and stub
handlers from Phase 1 mean Phase 2 mostly fills in handler bodies and adds a few new modules.

> **Picking this up in a fresh session?** Read, in order: this header → *Delivery approach* →
> *Increment 1 (as built)* (the engine's contract and the three real-world bugs it hit) →
> *Increment 2 (as built)* (the file pipelines and the isolation the PDF route depends on) →
> *Increment 3 (as built)* (images, and the format finding that route turns on) →
> *Usage-limit policy* (binding on every later increment) → *Writing Markdown: ranges take a
> hyphen* (binding on every prompt) → *The resume contract* (measured; it rules out the staging
> pattern increments 2–3 use) → *Increment 4 (as built)* (the review state machine increment 5
> builds on, and the rules that hold it together) → *Next up: increment 5*.
> Suite: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest` — **454 passing**, no network or model runs.

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
- [x] **2. Documents → Markdown** (`document_handler` + `files/`) — five routes: `.md` passthrough,
      `.txt`/`.csv` notes, `.pdf` conversion, unsupported → PDF-export reply. No review loop, no
      converter, no new dependency. **Shipped and owner-verified** — see "Increment 2 (as built)".
- [x] **3. Image → described note** (`image_handler`, VLM via `claude -p`) — still no review loop.
      **Shipped and owner-verified** — see "Increment 3 (as built)" below.
- [x] **4. Human-in-the-loop plumbing** (`core/session_store.py`, `handlers/conversation.py`,
      `core/review_poller.py`) — the review state machine, built and verified on a simple case
      (`#검토 <memo>`) rather than wired into audio. **Shipped** — see "Increment 4 (as built)".
- [ ] **5. Audio → meeting note** (`audio_handler`, `mlx-whisper`, glossary, review) — the most
      complex; depends on 1 and 4. **Next up** — the handoff is "Next up: increment 5" below.

Rationale: features 2–3 are one-shot and low-risk, so they validate the engine (1) before the
harder two-way review flow (4) and the STT-heavy audio pipeline (5) are attempted.

### Increment 2 (as built) — PDF → Markdown

Delivered: `handlers/document_handler.py` (five routes), `files/originals.py`, `files/text_files.py`,
`engine/prompts/pdf_to_markdown.md`, the `DOWNLOADS_DIR` setting, `ClaudeCLI.run(cwd=…, model=…)`,
and — during owner verification (below) — `ClaudeCLI.check_auth` behind an upgraded `claude-engine`
health probe, plus the PDF robustness changes (`CLAUDE_PDF_MODEL`, prompt hardening, output guard,
bounded retry). Suite: 171 → **270**.

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
  `CONVERSION_FAILED: <excuse>` must not. Empty output — and a too-short stub (`< 20` chars, a
  truncation that slips past the sentinel) — count as failure too.
- **PDF runs on `claude_pdf_model` (opus), not the cheap default, and retries.** Owner verification
  (round 2, below) found conversion *flaky* on `sonnet` for a real image-heavy deck. So the `.pdf`
  route: (1) runs on `claude_pdf_model` — opus by default, `CLAUDE_PDF_MODEL` to change — via the
  new `run(model=…)` override, while text enrichment stays on the cheap `claude_model`; (2) uses a
  hardened prompt that reserves the sentinel for a genuine `Read` failure and tells the model that a
  long/heavy document is normal, not a reason to bail; (3) **retries up to `_PDF_MAX_ATTEMPTS` (3)**
  when an attempt returns the sentinel or a stub. The retry is what stops a *flaky* bail from
  permanently skipping the PDF (a sentinel result is a normal `HandlerResult`, so the HWM would
  otherwise advance past it). It never launders a bad file: a genuinely unreadable one returns the
  sentinel on all three attempts and still ends in a reported failure, original filed.
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
path against the real CLI: no note, reason in the reply, original filed. Once the CLI was logged in
(round 2 below), the full PDF conversion was driven for real too.

**Owner verification, round 1 (the login gap).** The owner's first real PDF failed with
`Not logged in · Please run /login` — and a plain text memo then failed the same way, proving the
CLI's login had simply expired (this machine, not the pipeline). The code behaved correctly (no
fabricated note, original filed, reason relayed, no halt — a login failure is not `DeferMessage`
territory). But the `claude-engine` health probe had been green throughout, because it only checked
that the binary was *findable*. Closed here: the probe now also runs `claude auth status --json` and
reports **DEGRADED — logged out**. (Root cause of the expiry: a corporate PAC proxy —
`ProxyAutoConfigEnable` → an internal SKT host — that is unreachable off the office LAN, so token
refresh failed at home. That is the owner's device/network, not the bot.)

**Owner verification, round 2 (the flaky conversion — now closed).** Logged back in, the owner's
real 8-page/5.4MB proposal deck returned the `CONVERSION_FAILED` sentinel. Investigation (driving
the real CLI against a copy the owner staged at `/tmp/probe.pdf`) showed the file was perfectly
readable — a plain "just read it and report" prompt read all pages — but the *conversion invocation*
was **flaky on `sonnet`: 3 of 5 runs took the sentinel escape hatch, and one produced a 19-char
stub. `opus` converted it 2 of 2, then 3 of 3 through the full handler.** Fixes (all above under the
PDF bullet): `claude_pdf_model=opus` by default, a hardened prompt, the short-stub guard, and the
bounded retry. **PDF conversion quality is now owner-verified** — the resulting note carries the
document's own title, `##`/`###` structure, and rendered tables, faithfully.

## Scope

| Input | Output | Original file |
|-------|--------|---------------|
| Audio (m4a/wav/mp3/voice) | Meeting note (glossary-corrected, reviewed) → `0_inbox` | deleted after success |
| **Image** (screen capture/photo) | ✅ **shipped** — description + OCR note → `0_inbox` | **encoded into the note itself** (base64; heic→jpeg, bmp→png first) — no separate file |
| **PDF** | ✅ **shipped** — Markdown (Claude reads the PDF natively) → `0_inbox` | **moved to `~/Downloads/`** |
| docx / pptx / xlsx / … | ✅ **shipped** — **out of scope by decision**: reply asking for a PDF export | — |
| txt / csv | ✅ **shipped** — Markdown note (csv → table, rendered deterministically) → `0_inbox` | **moved to `~/Downloads/`** |
| Markdown (`.md`) | ✅ **shipped** — saved as-is → `0_inbox` | is the note |
| Text (upgrade) | ✅ **shipped** — LLM-enriched title/tags/summary (fallback = Phase 1 path) | — |
| **Text `#검토 …`** | ✅ **shipped (increment 4)** — drafted, reviewed over bot DM, then → `0_inbox`. The review loop's proving ground; opt-in, so a plain memo is unaffected | — |

## New modules

```
src/contextbot/
├── engine/                 # ✅ built in increment 1
│   ├── claude_cli.py       # wrapper over `claude -p` (async subprocess, JSON parse, resolution)
│   ├── parsing.py          # recover a JSON object from a model's free-text reply
│   └── prompts/            # text_enrich ✅ / pdf_to_markdown ✅ / image_describe ✅
│                           # review_draft ✅ / review_revise ✅ (increment 4) / meeting
├── core/
│   ├── session_store.py    # ✅ increment 4 — the pending review: draft, resume handle, work dir
│   └── review_poller.py    # ✅ increment 4 — bot-DM getUpdates, only while a review is open
├── handlers/
│   ├── audio_handler.py    # (fill in) audio → meeting note
│   ├── image_handler.py    # ✅ built in increment 3 — image → described note
│   ├── document_handler.py # ✅ built in increment 2 — md / txt / csv / pdf / unsupported
│   ├── downloads.py        # ✅ built in increment 3 — shared attachment download (audio is next)
│   └── conversation.py     # ✅ increment 4 — the whole review lifecycle: start, reply, end
└── files/                  # ✅ built in increments 2–3
    ├── originals.py        # original-file policy (Downloads / delete / keep-and-embed)
    ├── images.py           # ✅ increment 3 — heic/bmp → PNG, so the model actually *sees* it
    └── text_files.py       # encoding detection + deterministic CSV → Markdown table
```

Additional runtime deps: `mlx-whisper` (STT, Apple Silicon) — **and nothing else**. Documents need
no converter library and no LibreOffice: the scope is PDF-only and Claude Code reads PDFs natively
(see decision 3). Images need no imaging library: `sips` ships with macOS (increment 3). `ffmpeg` is
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

### Writing Markdown: ranges take a hyphen, never a tilde (binding on every prompt)

**Any prompt that makes the model produce Markdown *body* text must tell it to write ranges with a
hyphen** — `1분기-3분기`, `10-20명`, `2026-2027년` — never the `~` that Korean prose normally uses.
Carried today by `pdf_to_markdown.md` and `image_describe.md`; **increment 5's meeting-note prompt
will need it too**, and `test_prompts.py` asserts it per template so a new one cannot forget.

In GFM `~` is a strikethrough delimiter, and the failure is *pairing-based*, which is what makes it
nasty. Measured against MarkNotes' own renderer (`marked`, `gfm: true`):

| Input | Renders as |
|-------|-----------|
| `기간: 2026~2027년` (one tilde in the block) | fine — unpaired, so it stays literal |
| `참석자 10~20명, 예산 5~6천만원` | `참석자 10<del>20명, 예산 5</del>6천만원` ❌ |
| `\| 기간 \| 1분기~3분기, 10~20명 \|` (one table cell) | `1분기<del>3분기, 10</del>20명` ❌ |

**A single range renders fine.** So the bug is invisible until a paragraph or table cell happens to
carry two, at which point the text *between* them is silently eaten and both tildes disappear. You
cannot reason locally about it — hence "always a hyphen" rather than "avoid it when it pairs".

Two wrinkles the prompt wording has to handle:

- **It contradicts "transcribe faithfully".** The PDF and image prompts both insist on verbatim
  transcription, so they must say explicitly that the range separator is the *one* character allowed
  to change — the tilde is notation, not content, and swapping it is the only way the range survives
  rendering. Numbers, dates, and names still never change.
- **It must not be over-applied.** A `~` that is not a range — `~/Projects`, a URL, anything inside
  a code block (where inline formatting does not apply anyway) — stays exactly as it is.

**Why this cannot live in `CLAUDE.md` for the bot.** The owner's `~/.claude/CLAUDE.md` carries the
same rule for their own desktop/terminal Claude Code, but **the engine's hermetic flags mean it
never reaches `claude -p`**. Verified: a `CLAUDE.md` demanding a token in every reply was obeyed by
a plain `claude -p` and **ignored** under `--setting-sources ""` (the exact flags `ClaudeCLI` uses).
That is the hermeticism of *Increment 1 (as built)* working as designed — the bot's output cannot
drift with the owner's personal settings — and its price is that **every instruction the bot relies
on must be in the prompt template itself.** The two places are independent by construction, not by
oversight.

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

### Usage-limit policy — defer and replay (decided; implemented; **owner-verified for real**)

**When the usage limit is exhausted, the bot stops and leaves the message unprocessed. The owner
restarts after the limit resets, and catch-up replays it from where it left off.**

> **Confirmed against a real exhausted limit (owner, increment 3).** Everything below was designed
> against an *injected* 429 (`ANTHROPIC_BASE_URL` pointed at a local endpoint). The owner has since
> hit the **real 5-hour session limit** with `claude -p` genuinely unavailable, and **the policy
> behaved exactly as specified**: the message was deferred rather than half-processed, the HWM was
> left unadvanced, the bot halted with the reason, and the message was replayed intact after the
> window reset. So the 429 injection was a faithful stand-in, and the *lossless deferral* claim is
> no longer theoretical — it is the observed behaviour of the real limit, which is the one failure
> mode guaranteed to happen again on a Pro plan whose allowance is shared with the owner's own
> Claude Code sessions.

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
| Image (3) | ✅ **decided when built: save the image + a stub note that embeds it.** No fallback exists for the description — but unlike a PDF, the image *is* the capture and is worth keeping regardless, so the note is always written and always embeds it, with the reason in place of the description |
| Audio (5) | **No fallback.** STT is local, so the transcript survives — but the meeting note does not. Saving the raw transcript beats losing the recording. |
| Review (4) | ✅ **decided when built: the review always ends by delivering its draft.** A lost session, a review that cannot make progress, and an expiry all write the draft as a note marked unreviewed. A *recoverable* failure (usage limit, engine hiccup) keeps the review open instead — the transcript is on disk, so the owner just retries. Only `취소` discards. See "The open question — settled" |

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
probe and the PDF robustness changes (retry recovery, give-up-after-N, short-stub guard, the
`model=` override and `CLAUDE_PDF_MODEL`) — both added during owner verification — are covered in
`test_document_handler.py`, `test_claude_cli.py`, `test_health.py`, and `test_config.py`.
Suite: 171 → 270.

Increment 3 adds `test_image_handler.py` (the happy path, the staging isolation, the `제목:` line and
its fallbacks, every degradation path landing on a stub note that still embeds the image, and
deferral-leaves-no-side-effect), `test_images.py` (format policy + real `sips` conversions),
`test_downloads.py` (the extracted helper, incl. untrusted-filename traversal), and the
`CLAUDE_IMAGE_MODEL` cases in `test_config.py`. The image tests assert the bytes **decoded back out
of the note**, which is the only thing that proves the capture survived. Suite: 270 → **337**.

## Human-in-the-loop via session preservation

> ✅ **Shipped in increment 4** — this section is the design sketch that went in; *Increment 4 (as
> built)* is what came out, and it is the authority where the two differ (notably: the store's JSON
> file is authoritative rather than an in-memory dict, polling lives in its own
> `core/review_poller.py`, and every involuntary end **delivers** the draft rather than dropping it).

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
- **A resumable session is tied to the job's working directory** (`~/.claude/projects/<slugified-
  cwd>/<session-id>.jsonl`), so `temp_paths` above is not just tidiness — the review's directory
  must outlive the turn that created it or the session dies with it. See the measured resume
  contract under *Next up: increment 4*.

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

- **Shipped in increment 3** (see *Increment 3 (as built)* for the measurements behind each choice).
  The image is encoded **into the note itself** (`![…](data:image/jpeg;base64,…)`), above a
  Claude-generated description + OCR, in `0_inbox`. No separate image file, and so nothing to keep
  in sync with MarkNotes' `.assets/.metadata.json` ledger. No review loop.
- **`heic`/`heif`/`bmp` are converted to PNG first** (`files/images.py`, via macOS `sips`) — `Read`
  hands those back as raw *bytes* without erroring, and the model will describe the file header and
  report success. This is not optional.

### Increment 3 (as built) — image → described note

Delivered: `handlers/image_handler.py`, `files/images.py` (new — format normalization + inline
embedding), `handlers/downloads.py` (new — the shared download helper, extracted from
`document_handler`), `engine/prompts/image_describe.md`, and the `CLAUDE_IMAGE_MODEL` setting.
Suite: 270 → **337**.

**All four open decisions were settled by measurement before building.** Each is recorded below
with what was measured, because three of the four came out against the expected answer.

#### The four decisions

**1. Non-limit failure → keep the image, write a stub note that embeds it.** (Confirmed as the
handoff predicted.) The PDF route writes *no* note on failure, because a PDF without its conversion
is nothing. An image without its description is still **the image**, and it is what the owner
actually sent — so the note is always written, always opens with the embed, and carries
`> ⚠️ 이미지 설명을 생성하지 못했습니다 — <reason>` in place of the description. The capture is never
lost; only the searchable text is. This covers every non-limit path: engine error, the sentinel, an
unconvertible file, an oversized file, and `CLAUDE_ENABLED=false`.

**2. The image goes *inside* the note, base64-encoded: `![…](data:image/jpeg;base64,…)`.** The
handoff framed this as "`.assets/` vs note-adjacent" — a false choice, because **MarkNotes supports
a third form, and it is the right one for a bot.**

The first attempt shipped the `.assets/` route and was **wrong in a way the tests could not see**
(caught by the owner in review). Putting a file in `.assets/` is only half of that contract: the
folder's **`.metadata.json` is a ledger** — `{images: {<file>: {references: [...], uploadedAt,
size}}}` — that MarkNotes maintains to track which notes use which image and to clean up
unreferenced ones. A bot writing files in behind the app's back leaves that ledger not knowing they
exist. (`updateDocumentImageReferences` does self-heal on save, so it was recoverable — but
depending on the app to repair the bot's mess is not a design.)

The embedded form has **no such contract**: no file in `.assets/`, no metadata entry, nothing to
keep in sync. A note the bot writes is complete and self-contained the moment it lands — which is
exactly the property this bot wants, since it writes notes while the app is not looking. Verified
against the shape MarkNotes itself produces (`![<stem>](data:image/jpeg;base64,…)`, standard
Markdown), and round-tripped: the bytes decode back to the identical image.

**What `.assets/` buys is deduplication** — one file shared by several notes — and the owner's call
is that this vault is text-first and the reuse effectively never happens. Paying a ledger's
complexity for a saving that does not materialize is the wrong trade. *(Consequence: `ASSETS_DIR`,
`Settings.assets_dir`, `move_into_assets` and `embed_link` are all gone; the frontmatter carries no
`image:` key, because there is no file to point at and a path there could only ever be a lie.)*

**3. Image model: `sonnet` — the cheap default, measured, not assumed.** The handoff said *measure
before defaulting to opus*, and the measurement came back clean: **5 of 5 runs** on a real Korean
screenshot (mixed Korean/English, a 4-column table, a bold total row) produced an accurate
description and transcribed **every figure correctly**, in 2 turns for ~$0.025 est. The PDF route's
flakiness never appeared — an 8-page deck and one screenshot are not the same job. `sonnet` it is,
with `CLAUDE_IMAGE_MODEL` as the dial if a real photo ever proves harder. **No retry loop either**,
for the same reason: there was nothing flaky to absorb.

**4. Formats — and this is the one that matters.** `Read` **does not render `.heic` or `.bmp`, and
does not error on them.** Asked to read a `.heic`, it returns the file's **raw bytes**, and the
model — seeing `ftypheic` in the header — answered *"a HEIC image, likely a photo taken on an Apple
device"* with `is_error: False`. That is a confident, plausible, **entirely unseen** description
that would have been saved and trusted. It is increment 2's `.docx` fabrication wearing a different
hat: **the model describes what it can infer when it cannot see, and reports success either way.**
Pressed harder ("transcribe every line"), it did admit `READ_FAILED` — but the note would never have
pressed. Measured against the real CLI:

| Format | `Read` renders it? | MarkNotes renders it? | Policy |
|--------|--------------------|-----------------------|--------|
| `png` `jpg` `jpeg` `gif` `webp` | ✅ | ✅ | embedded as-is — **never re-encoded** |
| `heic` `heif` | ❌ **silently returns bytes** | ❌ | **→ JPEG** (a lossy photo stays lossy) |
| `bmp` | ❌ **silently returns bytes** | ❌ | **→ PNG** (lossless screen content) |

The two readers exclude the same formats, which is a useful coincidence: MarkNotes'
`ALLOWED_IMAGE_EXTENSIONS` is `.jpg .jpeg .png .gif .svg .webp`. So the converted image is what the
model reads **and** what the note embeds — keeping the `.heic` would have left a note whose image
the vault could not render either. `files/images.py` does this with **`sips`** (ships with macOS; no
new dependency, and the app is macOS-only already). `original_file` in the frontmatter still records
the true origin (`IMG_4821.heic`).

**The conversion target depends on the source, because the note now carries the bytes.** Measured on
a 12MP photo: a 1.85MB `.heic` → an **18.3MB PNG** (a **24MB note**) but a **4.0MB JPEG** (a 5.3MB
note). A camera photo is already lossy, so re-encoding it losslessly costs 10x and buys nothing; a
`.bmp` is the opposite — uncompressed screen content, where PNG is lossless *and* smaller. The first
draft converted everything to PNG, which was harmless when the image was a separate file and became
a 24MB liability the moment it went inline.

**Size cap: 10MB, applied *after* conversion** — MarkNotes' own `MAX_IMAGE_SIZE`, and base64
inflates by a further ~4/3, so 10MB of image is a ~13MB note. Checking the *original's* size (as the
first draft did) is meaningless: the `.heic` above passes at 1.85MB and then triples. Too big to
embed means there is no note worth writing, so the original is filed to `~/Downloads/` and the reply
says where it went — the same fallback as any other original.

#### As built

| Step | What happens |
|------|--------------|
| download | `handlers/downloads.download_attachment` into a temp dir |
| normalize | `files/images.normalize` — convert only if unrenderable (heic→jpeg, bmp→png) |
| cap | >10MB after conversion → no note; original → `~/Downloads/` |
| describe | one-shot `claude -p` on `claude_image_model`, staged alone (`cwd` + sole `--add-dir`) |
| write | the note, with the image base64'd into it — **the only side effect, after the LLM call** |

Points worth knowing before touching this:

- **The isolation is identical to the PDF route and equally mandatory** — staged alone in a
  `TemporaryDirectory` that is both `cwd` and the only `--add-dir`.
  `test_image_is_staged_alone_in_an_isolated_dir` asserts the directory listing *as the model would
  see it*, at call time.
- **The sentinel is `DESCRIPTION_FAILED`**, checked on the **first line** (a screenshot of an
  error-code table may legitimately contain the token), plus the same `< 20` chars short-stub guard
  the PDF route needs. Unlike the PDF route, a sentinel here does not mean "no note" — it means the
  stub note (decision 1).
- **The prompt asks for the title on a `제목:` line**, and the handler strips it off the body into
  the frontmatter. The first draft scraped the title from the description's first sentence instead,
  and the real-app run showed why that fails: descriptions are *sentences*, so the title came out
  60 characters long and cut off mid-word, with a filename to match. The model gives a title in the
  same call for free. A missing title line is not a failure — it falls back to the filename, then
  `이미지`, without discarding a perfectly good description.
- **A Telegram *photo* carries no file name at all.** Telethon's `_get_proper_filename` appends the
  media's real extension when the target path has none (`if not ext: ext = extension`) and returns
  the adjusted path — which is why `download_attachment` must use the **returned** path, not the one
  it asked for. That is what makes a photo arrive as a readable `.jpg` rather than an extensionless
  file the normalizer would try to convert. The test fake mirrors this deliberately; an earlier
  version did not, and its "photo" test passed while quietly shelling out to the real `sips`.
- **`_download`/`_safe_name` moved out of `document_handler`** into `handlers/downloads.py`
  (`download_attachment` / `safe_name`), per the handoff. It lives under `handlers/` rather than
  `files/` because it takes an `IncomingMessage`, which keeps the `handlers → files` dependency
  direction intact. Increment 5 is the third caller.
- **The note is the handler's only side effect**, which is what makes the deferral rule trivial
  here: there is no file to move, so a note either exists complete or does not exist at all.

**Verification status.** Driven for real, end-to-end through `handle_image` against the real
`claude` binary and the real `sips`, on a real Korean screenshot: both the `.png` path and the
`.heic` path produced an accurate description, a faithful table transcription (every figure
correct), a clean title, and an image that round-trips (the base64 decodes back to the identical
1000×620 picture). The suite itself runs no model and shells out to no `sips` from the handler
tests (`files/images.py`'s own tests do drive the real `sips`, which is local and offline — the
same reasoning that has `test_claude_cli.py` drive a real subprocess).

**Owner verification: ✅ done.** Photos sent from the real app become notes with the image visible
in MarkNotes. The `.assets/` correction above came out of that review; re-verified after the switch
to embedding.

**Two lessons worth carrying forward** (both are the same shape, and increment 4 should expect a
third):

1. **Reading the *consumer's* source beat guessing — but only where we thought to look.** MarkNotes'
   embed prefix and MIME map were read off its code and were right; the `.metadata.json` ledger sat
   in the same folder and was dismissed as bookkeeping because it happened to be empty. The tests
   could not have caught it: they assert *our* behaviour, and this was a contract with somebody
   else's app.
2. **Every "the model reports success" trap so far has been the same trap.** Increment 2: a blocked
   `.docx` → it converted a neighbouring file. Increment 3: a `.heic` → it described the file
   header. Both returned `is_error: False` with plausible output. **Assume the model would rather
   answer than admit it cannot see**, and design so it never gets the chance.

### The resume contract — measured against the real CLI, and it constrains the design

**Read this before touching the review loop.** `--resume` is what increment 4 stands on, and it
does not behave the way increments 2–3's staging pattern assumes. Probed against CLI v2.1.187:

| # | Situation | Result |
|---|-----------|--------|
| 1 | Resume from the **same cwd** | ✅ full context (`PURPLE-OTTER-42` recalled) |
| 2 | Resume after the cwd was **deleted** | ❌ `No conversation found with session ID: …` |
| 3 | Resume from a **different cwd** | ❌ same failure |
| 4 | Resume after **recreating the same path** | ✅ works — lookup is by path *string* |
| 5 | Resume in a **new process** | ✅ works (transcript is on disk) |
| 6 | `--session-id <uuid>` to **pin** the id | ✅ honoured; resumable by that id |
| 7 | Resume **twice** (turns 2 and 3) | ✅ **the id does not fork** — the same id comes back |

*(Row 7 was measured while building increment 4, and it is why the resume handle is written down
once and never refreshed: `ClaudeResult.session_id` on a resumed turn is the id you passed in.)*

**The transcript lives in `~/.claude/projects/<slugified-cwd>/<session-id>.jsonl`.** Sessions are
therefore scoped to the **working directory path**, and that is the trap:

> **Increments 2–3 run every job with `cwd` = a `tempfile.TemporaryDirectory` that is deleted the
> moment the handler returns.** Copy that pattern into a review and the session is unresumable
> before the owner has even read the question. Row 4 says a recreated path would work, but relying
> on the slug format is a hack — **give a review a stable directory** (e.g.
> `state/reviews/<message_id>/`) that lives until the review ends, and clean it up then.

That single fact settles several things for free: the staging **isolation** rule (stage the input
alone; `cwd` + sole `--add-dir`) still applies and still matters, but the directory must now be
*persistent-until-done* rather than a context manager. Row 5 is the good news — a review survives an
app restart, which is exactly what the on-disk state store is for. Row 6 is better news: **pin the
session id yourself**, and the state store can record a resumable handle *before* the call, so a
crash mid-call still leaves something to resume rather than an orphan.

**One failure mode, now typed.** A lost session surfaced as a bare
`ClaudeError: claude exited 1: No conversation found with session ID: …` — the CLI prints that as
**non-JSON**, so `run()` fell through to its exit-code branch. Increment 4 typed it as
**`ClaudeSessionLost`** (a `ClaudeError`, so existing callers still degrade), which is what lets the
state machine tell "this review is unrecoverable, hand the owner the draft" apart from "the engine
is briefly unhappy". Re-measured while building it, and the precise shape matters to the detection:
**exit 1, stdout empty, the message on _stderr_** — so it is matched there, lowercased, on
`no conversation found` alone.

**What increments 1–3 leave you (do not re-invent):**

- **`ClaudeCLI.run(…, session_id=…, resume=…)` and `resume_session` already exist** and are covered
  by `test_claude_cli.py` against a real fake-CLI subprocess. `ClaudeResult.session_id` is captured
  from the JSON. Both flags are measured above — the wrapper is ready; the *directory lifetime* is
  the part that is not.
- **`handlers/downloads.py`** is the shared download helper (increment 3 extracted it; audio is the
  third caller).
- **The reply bot is already there, and already async.** `core/client_service.py:62` builds
  `telegram.Bot(token)` and wraps it in `core/notifier.py`'s send-only `Notifier` (whose `BotLike`
  Protocol is what tests fake). python-telegram-bot 21.11.1 exposes
  `Bot.get_updates(offset=…, timeout=…, allowed_updates=…, limit=…)`, so the review poller needs no
  new dependency and no `Application`/`Updater` — just that call on the existing `Bot`, filtered to
  the owner id, while `AWAITING_REVIEW`. Keep `Notifier` send-only and put polling beside it rather
  than inside it; that split is what keeps capture unaffected by the 24h update-retention limit.
- **The hermetic flags mean `CLAUDE.md` never reaches the bot** — see *Writing Markdown: ranges take
  a hyphen* above. Any instruction a review turn depends on goes in the prompt template.
- **The side-effects-last rule gets harder, not easier.** A review spans *turns*: the draft is not
  the note until the owner accepts it, and the usage limit can now land on turn 2 rather than
  turn 1.

### Increment 4 (as built) — human-in-the-loop review plumbing

Delivered: `core/session_store.py` (the pending review), `core/review_poller.py` (bot-DM polling —
**a module the sketch above did not name**, because the split from `Notifier` earns its own file),
`handlers/conversation.py` (the whole review lifecycle), `engine/prompts/review_draft.md` +
`review_revise.md`, `ClaudeSessionLost`, the `REVIEW_EXPIRY_HOURS` setting, and the `ClientService`
wiring. Suite: 341 → **454**.

**The simple case it was built and verified on is `#검토 <memo>`** — the delivery approach's advice,
followed literally: no Whisper, no download, no staging, but the same multi-turn resume contract
end-to-end. **The audio pipeline is untouched.** A plain memo still takes the shipped one-shot path;
the review route is opt-in by prefix, dispatched in `route()` rather than inside `handle_text`, so
the two handlers stay independent of each other.

#### The open question — **settled: a review always ends by delivering its draft**

*(This was the "settle it first" item. Recorded here before any code was written; the "Increment 4
(as built)" section below is what came of it.)*

A failure mid-review strands a session in `AWAITING_REVIEW`. The answer starts by rejecting
`DeferMessage` outright — **not** as a judgement call, but because it does not typecheck against a
review turn:

- **There is no handler on the stack to replay.** The capture handler returned the moment it posted
  the review question. Turn 2 is driven by the *poller*, not by `_process`, so raising `DeferMessage`
  there would propagate out of a background task, not into the catch-up loop that knows what to do
  with it.
- **Even if it did, replay is the wrong operation.** It re-runs the whole job from scratch (for
  audio: Whisper again, minutes) to regenerate a draft **that already exists on disk**.
- **And halting is actively harmful here.** `_halt` stops the client — which stops the poller — so a
  review that halted itself could never receive the reply that would finish it. Deadlock.

So: **`DeferMessage` is for capture only. A review turn never raises it.** The asymmetry the
measurements create then splits the remaining cases cleanly, and they land on two answers, not one:

| Mid-review failure | Session on disk? | Answer |
|--------------------|------------------|--------|
| **Usage limit** (turn 2) | ✅ yes (row 5) | **Stay in `AWAITING_REVIEW`.** Tell the owner, keep polling, let them re-send their reply after the window resets. Nothing is lost and nothing is written. |
| Transient engine error (timeout, CLI hiccup) | ✅ yes | Same — stay, report, let the owner retry. Bounded (below). |
| **`ClaudeSessionLost`** | ❌ **nothing to resume** | **Deliver the draft** as an unreviewed note and end the review. |
| N consecutive failures on one review | probably not | **Deliver the draft** and end — the give-up that stops "stay and retry" from stranding forever. |
| Review expiry (owner never replied) | ✅ but moot | **Deliver the draft** and end. |

**The unifying rule: a review never ends empty-handed.** Three of those five rows converge on the
same exit, which is the whole answer to *"never let it strand a draft"* — an unrecoverable review
ends by **giving the owner the draft**, not by discarding it. Only the *reviewed* quality is lost,
never the work. This is increment 3's decision 1 (an image without its description is still the
image) applied one level up: **a draft without its review is still a draft**, and it is the expensive
artifact — a Whisper run plus an LLM pass. The PDF route's "no note at all" reasoning does not
transfer, because a PDF without its conversion is genuinely nothing, whereas a draft is content.
Delivered drafts are marked, so an unreviewed note can never be mistaken for an accepted one.

**Why a usage limit does *not* halt the bot** (the one place this departs from the usage-limit
policy's reflex): the limit is real, so the *capture* path will hit it on its very next message and
halt through the existing route. Halting from the review turn adds nothing and costs the poller —
i.e. the owner's ability to retry. The policy's table is unchanged; it just never applied here,
because a review turn is not a capture.

**Two consequences worth stating plainly, because they are easy to get backwards:**

- **The HWM advances when the review is *recorded*, not when the note is written.** A review-bearing
  job is "processed" once its draft and resume handle are durably on disk — from there the *store*
  owns the review's lifetime, not the watermark. The alternative (hold the HWM until the owner
  accepts) is wrong twice over: catch-up breaks at the first unadvanced message, so an unanswered
  review would block every later capture while the owner takes an hour to reply, and a restart would
  re-run the whole job on top of a perfectly good draft. The HWM means *ingested*, and always did.
- **`취소` is the one case that discards.** The intent is unambiguous and the loss is recoverable:
  Saved Messages is the durable input, so the owner re-sends. Every *involuntary* end delivers.

#### As built

The state machine, and every edge out of it:

| From | On | To |
|------|----|----|
| IDLE | `#검토 <memo>` → draft + questions | **AWAITING_REVIEW** |
| AWAITING_REVIEW | `확인` / `ok` / `네` / `저장` | note written (`reviewed: true`) → IDLE |
| AWAITING_REVIEW | `취소` | **draft discarded** → IDLE (the only discard) |
| AWAITING_REVIEW | anything else | FINALIZING → resume → new draft → **AWAITING_REVIEW** (loop) |
| AWAITING_REVIEW | usage limit / engine hiccup | stays **AWAITING_REVIEW** — retry by re-sending |
| AWAITING_REVIEW | `ClaudeSessionLost` / 3 consecutive failures / 24h expiry | draft delivered (`reviewed: false`) → IDLE |
| *(crash during turn 1)* | next start: `discard_incomplete` | IDLE — the memo replays, HWM never advanced |
| *(crash during a later turn)* | next start: `discard_incomplete` clears the stale FINALIZING | **AWAITING_REVIEW** — the draft is intact |

A usage limit does **not** count toward the 3-failure give-up: the engine is not broken, the
allowance is spent, and counting it would abandon a healthy review after three retries inside one
limit window. Expiry is the backstop that keeps "stay and retry" from waiting forever.

Points worth knowing before touching this:

- **The work dir is `state/reviews/<message_id>/work/`, and the draft is one level *above* it.**
  Both halves matter. The dir is the session's cwd (so it must outlive the turn — the whole reason
  the `TemporaryDirectory` pattern is banned here), *and* it is the model's view of the filesystem
  (so increment 2–3's isolation rule still binds). Keeping the draft outside it is what lets both be
  true at once: `test_the_work_dir_is_empty_so_the_isolation_rule_still_holds` asserts the listing.
  For `#검토` the dir is simply empty — which is the strongest isolation available. **Increment 5
  stages the audio/transcript there, alone.**
- **The session id is pinned and recorded *before* the first call** (row 6), and written once —
  resuming does not fork it (row 7).
- **Three ways turn 1 can end, and they need three different unwinds** — this is where building it
  corrected the design:
  - **`DeferMessage` (usage limit) → remove the entry.** The replay re-runs the handler from
    scratch, so a review left behind would bounce that replay off the one-at-a-time guard and
    answer the memo with "이미 검토 중" forever.
  - **Any other exception → remove the entry and re-raise.** We are still alive, so we can unwind;
    leaking here would block every later review on a draft that does not exist.
  - **A crash → nothing runs, so `discard_incomplete` clears it at the next start.** The signal is
    the draft *file*, which only a completed turn creates.
- **What pinning the id early actually buys is narrower than it first looks — and the honest
  version matters.** The sketch said it means "a crash mid-call leaves something to resume". It
  does, but **resuming it is not useful**: there is no draft, and the owner was never asked
  anything. The real recovery is simpler and already correct — `_process` advances the HWM only
  *after* the handler returns, so a crash during turn 1 leaves the memo unprocessed and catch-up
  replays it. What pinning genuinely buys is that the handle is always *knowable*, so the entry can
  be cleaned up deterministically instead of leaking, and so turns 2+ have a handle that was never
  in doubt.
- **`SessionStore`'s JSON file is the authority, not an in-memory dict.** The capture handler and
  `ClientService` each hold their own store over the same path; if either cached, one could open a
  review the other could not see, and the client would never start polling. Every operation loads,
  mutates, and atomically writes. (This is a deliberate departure from the "in-memory + on-disk"
  sketch above — the file is tiny and touched at human speed, so the cache buys nothing and costs a
  coherence bug.) The client asks the *store* whether a review is open rather than reading it off a
  `HandlerResult`, so handlers need no new channel to announce one.
- **At most one review at a time**, enforced in the store. With two open, a bare `확인` in the bot DM
  is unattributable, and guessing would silently apply the owner's answer to the wrong draft. A
  second `#검토` replies "먼저 끝내주세요" and advances — *not* `DeferMessage`, which would halt the
  bot, stop the poller, and leave the open review permanently unanswerable. **This is the constraint
  increment 5 has to revisit** (below).
- **The poller filters on two things, and the second is the one that matters.** Long polling returns
  whatever Telegram retained (24h), so a reply is accepted only if it is from the owner's DM **and
  sent after the review began**. Without the timestamp check, a message the owner typed at the bot
  *before* the review would be read as their answer to a question they had not yet been asked. The
  cutoff is the **review's** start, not the poller's — otherwise a restart would silently discard an
  answer sent while the app was closed.
- **The review block is bounded as a *whole*, and the draft is what yields.** Telegram rejects a
  message over 4096 chars and `Notifier` *swallows* the rejection (it must never let a failed reply
  break processing), so an oversized block is one the owner never sees while the store waits for
  their answer — an unanswerable review, silently. Budgeting only the draft leaves the total
  unbounded, since the title, the questions, and the footer ride along; the questions are kept whole
  (they are what the owner must answer) and the draft gives up the room, because the draft is the
  part they can read in the note.
- **`FINALIZING` must not outlive the process that set it.** It guards against two CLI processes
  resuming one transcript, but it is persisted — so a crash mid-turn left it set forever, and every
  later reply (**including `확인` and `취소`**) was answered "잠시 후 다시 보내주세요" until the 24h
  expiry. The owner could not rescue their own draft. `discard_incomplete` clears it: no turn can be
  in flight in a fresh process.
- **`review.write_draft` replaces the draft wholesale each turn**, which is why `review_revise.md`
  insists on the complete note rather than a diff. The safety net is structural rather than a
  guard: **every revision is shown to the owner before it can become a note**, so a truncated one is
  visible, not silent.
- **The sentinel is `DRAFT_FAILED`**, first-line-matched, with the same `< 20` chars short-stub
  guard the PDF and image routes use, for the same reason.
- **A failure at turn 1 never loses the memo**: no draft is possible, so the memo is saved as an
  ordinary note with the reason in the reply. `CLAUDE_ENABLED=false` takes the same path.
- **`확인!` is `확인`.** Accept/cancel matching strips trailing punctuation, because without it the
  reply falls through to a *revision* — spending a full LLM turn rewriting the note against the
  "instruction" `확인!`, at the exact moment the owner thought they were done.

*(The three bullets above came out of a review of this increment's own diff, not the design. Two of
them — the unbounded block and the immortal `FINALIZING` — were silent strandings of exactly the
kind the state machine exists to prevent, reintroduced by the machinery meant to prevent them.)*

**The third trap did not materialize — and that is worth recording.** Increments 2 and 3 each hit
the same shape (the model answers plausibly rather than admitting it cannot see, `is_error: false`),
and increment 4 was expected to hit a third. The natural candidate was real: if `--resume` were to
*silently start a fresh session*, the model would produce a confident "revision" from the reply
alone, with nothing to reveal that the draft it claims to be revising was never in its context.
**Measured: it does not.** A lost session fails loudly (exit 1, `No conversation found`), which is
what made typing it possible at all. The trap's absence here is a property of the CLI, not of our
care — so the assumption stays for increment 5.

**Verification status.** Driven for real, end-to-end through `handle_review_request` /
`handle_reply` against the real `claude` binary, on a Korean memo:

- **The full loop works**: draft (title + 4 questions) → correction ("담당자는 김철수 책임, 기한은
  7월 24일, 캐시 서버 건은 보류가 아니라 취소") → resume applied all three, **dropped the questions it
  had answered, kept the ones still open** → `확인` → note written with `reviewed: true`.
- **The tilde rule survives the hermetic flags.** The memo deliberately carried two ranges written
  the Korean way (`20~30%`, `10~15%`) — both came back as `20-30%` / `10-15%`. The rule reached the
  model through the prompt template, which is the only route it has (`CLAUDE.md` never gets there).
- **The session-lost path was driven for real**, not simulated: the transcript was located by
  session id under `~/.claude/projects/` and deleted, then the owner's reply resumed into a dead
  session. `ClaudeSessionLost` was raised from the real stderr, the draft was delivered with
  `reviewed: false` and its banner, and the review closed. *(Deleting the work dir alone is **not**
  enough to test this — row 4: the lookup is by path string, so anything that recreates the path
  restores resumability. The first attempt at this probe proved only that.)*
- **Turn 1's deferral was verified against a real exhausted limit**, not an injected 429 — the
  probing itself spent the 5-hour allowance, which is the usage-limit policy's own point about
  sharing the pool. `handle_review_request` raised `DeferMessage`, and the store was left with
  `{"reviews": []}` and no note: **the unwind ran on the real path**, so the replay will meet a
  clean guard rather than "이미 검토 중".

*(The three review fixes above landed after those runs. All three are pure string/state logic with
no engine call in them — the turn-1/turn-2 engine interaction is byte-for-byte what was driven — and
each is unit-covered.)*

**Owner verification: pending.** The bot-DM poller is the one part no unit test can prove, since it
is the real Bot API's behaviour. Everything else above ran for real.

### Next up: increment 5 — audio → meeting note

**Status: not started.** It is the last one, the most complex, and the only one that depends on two
prior increments (1 and 4).

Scope: `handlers/audio_handler.py`, `mlx-whisper` STT, the shared glossary, and the review loop —
see *A. Audio → meeting note* above for the pipeline, and *Increment 4 (as built)* for the state
machine it plugs into.

**What increment 4 leaves you (do not re-invent):**

- **`conversation.start`-shaped work is already done.** `handle_review_request` is the *producer*
  pattern to copy: pin a session id → `store.create(...)` **before** the call → run turn 1 with
  `cwd`/`add_dirs` = `review.work_dir` → `review.write_draft(body)` → `store.update(review)` →
  return the review block as the reply. Everything after that — the poller, `handle_reply`, revise,
  accept, cancel, expiry, delivery — is shared and needs nothing from audio.
- **Stage the audio and the transcript in `review.work_dir`, alone.** It already exists, it is
  already the cwd, and it is already empty. The glossary is the one exception: it is read-only
  context from *outside*, so it is a second `--add-dir`, never a copy into the work dir.
- **`handlers/downloads.download_attachment`** is the shared download helper — audio is its third
  caller, as increment 3 predicted.
- **`mlx-whisper` and `ffmpeg` will hit the launchd-PATH wall** that `claude` hit (increment 1).
  Resolve them like `resolve_executable` does; do not assume PATH.

**The decisions increment 5 has to make (each one is genuinely open):**

1. **One review at a time is going to hurt here, and this is where it gets designed.** A `#검토`
   memo bouncing off "먼저 끝내주세요" costs a re-send. **An audio file bouncing off it costs a
   re-upload of a large recording, and the whole Whisper run.** The constraint exists because a bare
   `확인` cannot be attributed across two open reviews — so the fix is *not* "allow two", it is to
   decide between a **queue** (capture drafts it, holds it, asks when the current review ends) and
   **addressing** (each review block carries a handle the reply must name). Note the queue defers
   the *asking*, not the job — the transcript is already made, so nothing expensive is repeated.
2. **The replay cost, now unavoidable.** A usage limit on turn 1 raises `DeferMessage`, and the
   replay re-runs the handler from scratch — for audio that is Whisper again, minutes. Either cache
   the transcript keyed by message id, or accept the rework. Increment 4 makes one thing easier:
   turn 1 is the *only* place `DeferMessage` can fire, because a review turn never raises it.
3. **The glossary's git commit/push** (open decision 2) and **sharing vs copying the glossary file**
   (open decision 5) — both still open, both increment 5's.
4. **The audio's own side effects.** The original is deleted on success — and "success" now means
   *the owner accepted the note*, not *the draft was made*. That is turns apart from the handler
   that downloaded it, and `deliver_draft` can end a review from a background task. Whatever holds
   the audio must be reachable from there, or the deletion has to be given up on deliberately.

**Verification bar (same as increments 2–4):** unit tests with a mocked engine and mocked STT (no
model runs, no model downloads, no network), the whole suite green, then a real-app run.

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

Audio → delete after success · **Image → encoded into the note (`files/images.to_data_url`,
increment 3); no original kept, and nothing for this module to do — unless it is too large to
embed, which falls back to `~/Downloads/`** · pdf/txt/csv → move to `~/Downloads/`
(`move_to_downloads`) · Markdown → save to inbox (it *is* the note).

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

Increment 4 adds `test_conversation.py` (the state machine: the draft turn, the resume turn, accept
/ cancel / revise, and **every failure edge** — a usage limit on turn 1 deferring *and leaving no
review behind*, a usage limit on turn 2 keeping the review alive, a lost session delivering the
draft, the give-up counter, expiry), `test_session_store.py` (the work-dir isolation, the file as
authority across two store instances, restart survival, `created_at` vs `source_date`), and
`test_review_poller.py` (the owner filter, **the backlog filter and the restart case it protects**,
offset advance, the tick, transport errors). `ClaudeSessionLost` is covered in `test_claude_cli.py`
against the fake-CLI subprocess, the two review templates in `test_prompts.py` (including the tilde
rule, which is parametrized per template so a new one cannot forget it), the prefix dispatch in
`test_router.py`, and the poller lifecycle + the HWM-advances-on-review rule in
`test_client_service.py`. Suite: 341 → **454**.
