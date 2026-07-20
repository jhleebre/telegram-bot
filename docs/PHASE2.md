# Phase 2 — Development Plan (all 5 increments shipped)

Phase 2 adds the LLM/VLM-powered pipelines on top of the Phase 1 skeleton. The engine is
**Claude Code in headless mode** (`claude -p`), and multi-turn human-in-the-loop review is built
on **Claude Code resumable sessions** (`--session-id` / `--resume`). The routing table and stub
handlers from Phase 1 mean Phase 2 mostly fills in handler bodies and adds a few new modules.

> **Picking this up in a fresh session?** Read, in order: this header → *Delivery approach* →
> *Increment 1 (as built)* (the engine's contract and the three real-world bugs it hit) →
> *Increment 2 (as built)* (the file pipelines and the isolation the PDF route depends on) →
> *Increment 3 (as built)* (images, and the format finding that route turns on) →
> *Usage-limit policy* (binding on every pipeline) → *Writing Markdown: ranges take a hyphen*
> (binding on every prompt) → *The resume contract* (measured; it rules out the staging pattern
> increments 2–3 use) → *Increment 4 (as built)* (the review state machine) → *The decisions,
> settled* → *Increment 5 (as built)* (audio, the queue, the bugs only real runs found, and the
> window).
> Suite: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest` — **716 passing**, no network or model runs.
>
> **Phase 2 is complete.** Every pipeline in *Scope* is shipped and owner-verified, and every open
> decision is settled — the last was **6**, closed after the merge: *the bot DM now answers every
> message while the app is up*, which is the only offline signal Telegram makes available (there is
> no autoresponder to borrow; a bot is a token plus your code). Silence therefore means exactly one
> thing: nothing is running.
>
> One thing is deliberately **not** built: a plain message to the bot DM does not *start* anything.
> It reports and points at Saved Messages. Increment 5 deleted `#검토`, so **text has no interactive
> path today** — a Saved Messages memo is always a quick note, which is the owner's stated model.
> That was a decision, not a side effect; the plumbing for the other choice is now in place.
>
> **Three lessons, one shape, five increments.** *Read the consumer's source; the model would rather
> answer than admit it cannot see; the failures here are silent.* Increment 5 hit all three again:
> the vault (not this doc) knew the note type **and** the filename convention, `mlx_whisper`'s source
> (not its docs) knew about the network call and the hardcoded `ffmpeg`, and not one of its own
> seven bugs raised anything. Assume a sixth increment would meet them a sixth time.
>
> **And a fourth, earned by the window:** *a constant that overrides what a system already knows
> will not fail loudly — it will quietly produce something almost right.* A minimum size below what
> the layout needs clips the content instead of shrinking the window; a health check fired before
> the client connects reports a login failure instead of "not yet". Both looked like the code was
> working.

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
      (`#검토 <memo>`) rather than wired into audio. **Shipped and owner-verified** — see
      "Increment 4 (as built)". The `#검토` route is **scaffolding that increment 5 deletes**, as
      its last step; the removal boundary is written out in the increment 5 handoff.
- [x] **5. Audio → meeting note** (`audio_handler`, `mlx-whisper`, glossary, review) — the most
      complex; depends on 1 and 4. **Shipped** — see "Increment 5 (as built)" below. It also
      **deleted the `#검토` scaffolding** as its last step, so audio is now the review loop's only
      producer.

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
- **`.md` filenames are normalized** to the vault's `YYMMDD-<분류>-<slug>.md`, so a sent note sorts
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
| **Audio** (m4a/wav/mp3/voice) | ✅ **shipped (increment 5)** — local Whisper → glossary-corrected, reviewed meeting note (`type: meeting-note`) → `0_inbox`. A second recording is **queued**, not bounced | **deleted when the review ends** — it lives in the review's tree, so nothing has to remember |
| **Image** (screen capture/photo) | ✅ **shipped** — description + OCR note → `0_inbox` | **encoded into the note itself** (base64; heic→jpeg, bmp→png first) — no separate file |
| **PDF** | ✅ **shipped** — Markdown (Claude reads the PDF natively) → `0_inbox` | **moved to `~/Downloads/`** |
| docx / pptx / xlsx / … | ✅ **shipped** — **out of scope by decision**: reply asking for a PDF export | — |
| txt / csv | ✅ **shipped** — Markdown note (csv → table, rendered deterministically) → `0_inbox` | **moved to `~/Downloads/`** |
| Markdown (`.md`) | ✅ **shipped** — saved as-is → `0_inbox` | is the note |
| Text (upgrade) | ✅ **shipped** — LLM-enriched title/tags/summary (fallback = Phase 1 path) | — |
| ~~**Text `#검토 …`**~~ | ⛔ **deleted (increment 5)** — it was the review loop's proving ground and nothing more. Audio is the producer now, and a magic prefix in the *capture* channel made "throw it in and it becomes a note" conditional. See *Increment 5 (as built)* | — |

## New modules

```
src/contextbot/
├── engine/                 # ✅ built in increment 1
│   ├── claude_cli.py       # wrapper over `claude -p` (async subprocess, JSON parse, resolution)
│   ├── parsing.py          # recover a JSON object from a model's free-text reply
│   └── prompts/            # text_enrich ✅ / pdf_to_markdown ✅ / image_describe ✅
│                           # review_revise ✅ (4) / meeting_note ✅ meeting_glossary ✅ (5)
│                           # (review_draft.md deleted with the #검토 scaffolding)
├── core/
│   ├── session_store.py    # ✅ increment 4 — the review: draft, resume handle, work dir
│   │                       #    ✅ increment 5 — + the queue (QUEUED), note_type, tags
│   └── bot_dm_poller.py    # ✅ increment 4 — bot-DM getUpdates; polls whenever the app is
│                           #    up (decision 6), reporting the backlog rule rather than dropping
├── handlers/
│   ├── audio_handler.py    # ✅ increment 5 — audio → meeting note; the review loop's producer
│   ├── image_handler.py    # ✅ built in increment 3 — image → described note
│   ├── document_handler.py # ✅ built in increment 2 — md / txt / csv / pdf / unsupported
│   ├── downloads.py        # ✅ built in increment 3 — shared attachment download (audio is 3rd)
│   └── conversation.py     # ✅ increment 4 — the whole review lifecycle: reply, revise, end
│                           #    ✅ increment 5 — + activate/promote_next, and accept's side effects
├── stt/                    # ✅ increment 5 — ported from meeting-transcriber, not imported from it
│   └── whisper.py          # transcribe() + model presence; ffmpeg put on PATH, not just resolved
├── ui/                     # ✅ Phase 1, rebuilt by increment 5 into a compact status bar
│   ├── main_window.py      #    one row, expanding to HEALTH + ACTIVITY (was 533x620 of card)
│   ├── icon_buttons.py     # ✅ increment 5 — play/stop + chevron, painted (a font is not a shape lib)
│   ├── elided_label.py     # ✅ increment 5 — a QLabel that ends in `…` instead of clipping
│   └── bot_worker.py       #    Qt thread ↔ asyncio loop; asks for health when start() resolves
│                           #    (status_widget.py deleted with the pulsing status face)
├── localtime.py            # ✅ post-5 fix — the clock a note is written on (Telethon reports UTC)
└── files/                  # ✅ built in increments 2–3
    ├── originals.py        # original-file policy (Downloads / delete / keep-and-embed)
    ├── images.py           # ✅ increment 3 — heic/bmp → PNG, so the model actually *sees* it
    ├── glossary.py         # ✅ increment 5 — read/append the vault's term table (never commits)
    ├── audio_meta.py       # ✅ post-5 fix — when a recording's metadata says where it was made
    └── text_files.py       # encoding detection + deterministic CSV → Markdown table
```

*(`stt/` was a suggestion and increment 5 kept it. The point it encodes held: the STT code is
**ported in**, so the bot never depends on `~/Projects/meeting-transcriber/` at runtime. The
glossary is the one genuine cross-project file, and it moved into the **vault** rather than staying
there — see *The decisions, settled* → 4.)*

Additional runtime deps: `mlx-whisper` (STT, Apple Silicon) — **and nothing else**. Documents need
no converter library and no LibreOffice: the scope is PDF-only and Claude Code reads PDFs natively
(see decision 3). Images need no imaging library: `sips` ships with macOS (increment 3). `ffmpeg` is
already present (`/opt/homebrew/bin` — so it must be *resolved*, not assumed on PATH). `claude` CLI
is already installed (v2.1.187 verified).

**`mlx-whisper` is not installed in this project's venv yet** (it lives in meeting-transcriber's),
and it brings a **1.5GB model** that is not a package dependency at all — `mlx_whisper` fetches it
from HuggingFace on first use. Both are increment 5's to own; the measured details are in *Next up:
increment 5 → The STT dependency*.

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

The lesson generalizes past this one binary, and it has since been **confirmed by measurement**:
increment 5's `ffmpeg` really does live in `/opt/homebrew/bin`, so it hits exactly the same wall
from the Dock. Resolve it the same way rather than assuming PATH — see the STT dependency table
under *Next up: increment 5*, which also records the same shape of trap for the Whisper *model*
(cached here, absent on a fresh machine, downloaded silently mid-job).

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

> ✅ **Shipped in increment 5** — this is the sketch that went in; *Increment 5 (as built)* is what
> came out and is the authority where they differ. Notably: the audio is staged **outside** the work
> dir (the model cannot hear it, and a file it would `Read` into raw bytes is the `.heic` trap), the
> glossary lives in the **vault** rather than meeting-transcriber, and the note is `meeting-note`.

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
**a module the sketch above did not name**, because the split from `Notifier` earns its own file;
**renamed `core/bot_dm_poller.py`** when open decision 6 made it poll all the time — "review" was
its activation rule, not its identity),
`handlers/conversation.py` (the whole review lifecycle), `engine/prompts/review_draft.md` +
`review_revise.md`, `ClaudeSessionLost`, the `REVIEW_EXPIRY_HOURS` setting, and the `ClientService`
wiring. Suite: 341 → **474**.

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
- **Talking to the bot with no review open does nothing, silently.** Nothing polls when the store
  is empty, so the message sits in Telegram's queue; when a review next opens, the first
  `get_updates` runs with **no offset** and pulls the whole retained backlog, where the timestamp
  filter drops it for predating the review. That is the *correct* outcome and not a small one — a
  stray `확인` typed at the bot yesterday would otherwise instantly accept a draft the owner had
  never seen. But the owner gets **no reply either way**, so the bot reads as broken. **This is the
  natural home for a "talk to the bot to start something" feature** (it needs the poller running
  whenever the app is up — cheap, and safe *because* the bot DM is not the capture channel), and
  it is the open end of the "capture vs conversation" split. Not built; not increment 4's job.
- **The clock starts when the owner is asked, not when the work began** — `created_at` is stamped
  provisionally at `store.create()` and re-stamped once the draft is ready. The draft turn sits in
  between, and it is ~60s for a memo but **minutes for audio**; anything typed at the bot during it
  predates the question, so it cannot be an answer and must not pass the filter. **Turn 1 only** —
  a revision must *not* re-stamp, because during a revise turn the poller is running and the
  `FINALIZING` guard already answers a concurrent message with "잠시 후 다시 보내주세요". Re-stamping
  there would silently drop a correction the owner really did send, which is strictly worse.
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
- **The trigger is a whole word, not a `startswith`.** `#검토된 사항 정리` is a memo *about* something
  reviewed; a bare prefix match diverted it into the review loop **and** handed the model
  `된 사항 정리`, drafting a note from mangled text. A Markdown `# 검토 …` heading has a space after
  the hash and was never affected.

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

**Owner verification: ✅ done.** The owner drove the `#검토` loop in the real app and it passed —
which is what closes the **bot-DM poller**, the one part no unit test can prove because it is the
real Bot API's behaviour talking to a real phone. Everything else above had already run for real.

**Four bugs came out of the work *after* that verification** — three from reviewing this
increment's own diff, one from the owner asking what happens if you talk to the bot with no review
open. All four are fixed and covered; they are listed in the "as built" bullets above. The pattern
is worth naming, because increment 5 will meet it: **every one was a silent failure**. Nothing
raised, nothing logged, the suite stayed green — a mangled memo, an undeliverable question, a
permanently wedged review, an impatient "얼마나 걸려?" applied as a correction. A review loop's
failures are quiet by construction, because the only witness is a person reading a DM.

### The increment 5 handoff (kept — it is the *why*, and every prediction in it is now settled)

> ✅ **Shipped.** This was the handoff written before the work; **Increment 5 (as built)** below is
> what came of it and is the authority where they differ. It is kept in full rather than trimmed,
> because it is the record of what was known in advance — and it earned that: the STT dependency
> table, the removal boundary, and all five decisions it posed held exactly as framed. Two of its
> incidental claims did not survive contact (`type: meeting`, and staging the audio in the work
> dir); both are corrected in *as built*, and both were caught by reading the source rather than by
> a test.

Scope: `handlers/audio_handler.py`, `mlx-whisper` STT, the shared glossary, and the review loop —
see *A. Audio → meeting note* above for the pipeline, and *Increment 4 (as built)* for the state
machine it plugs into.

#### First: delete the `#검토` scaffolding — **as increment 5's last step, not its first**

> ✅ **Done, and last, exactly as argued.** The boundary table below was followed symbol by
> symbol, and both reasons for the ordering paid: `#검토` was the control group that answered
> "my code or the plumbing?" while the audio pipeline was driven for real, and deleting it
> first would have left increment 4 unreachable from the app. Its tests went with it, which is
> why the suite drops from 601 to 573.

**`#검토` is scaffolding, and increment 5 is where it dies.** It exists only because the state
machine had to be built and verified before audio existed (the delivery approach's advice). It is
now **owner-verified and has no remaining purpose**: the moment audio is the producer, a magic
prefix in the *capture* channel is a wart. Saved Messages means "throw it in, it becomes a note" —
`#검토` makes that semantics conditional, which is exactly the design that produced the
`#검토된 사항` mangling bug above. **Owner's call, recorded here so it cannot ossify into a feature
by accident.**

**Delete it last, not first**, for two reasons — the second decides it:

1. It is the **control group** while you build. `#검토` exercises the whole review loop in the real
   app in seconds with **no Whisper in the path**, so "is it my new code or the existing plumbing?"
   has a cheap answer. Increment 5 *does* touch the shared path (glossary side effects at accept
   time; possibly `handle_reply` for the queue), so that question will come up.
2. Deleting it first leaves **increment 4 unreachable from the app**. `#검토` is currently the only
   producer of a review, so removing it before audio is wired makes the store, the poller, and the
   conversation module ~700 lines that nothing in the running app can open. The suite stays green
   (its tests call `handle_reply` directly), which is precisely what makes that state dangerous.

**The removal boundary** — recorded now, while the increment-4 context is fresh, so this is a
mechanical delete rather than an archaeology exercise:

| Delete (scaffolding) | Keep (shared plumbing) |
|----------------------|------------------------|
| `REVIEW_PREFIX`, `is_review_request`, `strip_prefix` | `SessionStore` / `PendingReview` / `ReviewState`, whole |
| `handle_review_request` — replaced by the audio producer | `handle_reply`, `_revise`, `_stay`, `deliver_draft`, `expire_stale`, `discard_incomplete` |
| `_plain_note` (memo-specific fallback) | `review_block`, `parse_draft`, `_has_questions`, `_elide`; **`_write` — but see below, it is not media-neutral yet** |
| the `is_review_request` branch in `core/router.py:route` | `core/review_poller.py`, whole; the `ClientService` wiring |
| `engine/prompts/review_draft.md` | `engine/prompts/review_revise.md` — **turn 2+ is media-agnostic** |
| in `test_conversation.py`: `test_is_review_request`, the trigger/prefix cases, `test_a_plain_memo_never_enters_the_review_loop`, `test_engine_off_saves_a_plain_note…`, `test_a_failed_first_draft_still_saves_the_memo`, `test_a_memo_starting_with_a_similar_word_is_not_mangled` | every test from `_open_review` onward — rebuild the fixture on the audio producer and they all still apply |
| in `test_router.py`: the two `#검토` dispatch cases | — |

**The contract the audio producer must honour** (it is what the kept code above assumes):

- Turn 1's output parses through `parse_draft`: a `제목:` first line, the body, then the questions
  under the `QUESTIONS_HEADING` (`## 확인 요청`). **So the meeting-note prompt must carry
  `questions_heading` and the `제목:` line**, the same way `review_draft.md` does — copy that
  structure, not its wording. `review_revise.md` then works unchanged, because a revision turn does
  not care whether the draft came from a memo or a recording.
- `- (없음)` under the heading means "nothing to ask" — for audio that is the flagged-glossary-terms
  list being empty.
- **`_write` hardcodes `note_type="note"` and `tags=[]`**, which is the memo's answer smuggled into
  shared code. A meeting note wants `type: meeting`, so the producer has to own this: add
  `note_type` (and tags, if the meeting prompt supplies them) to `PendingReview` and pass it
  through. `from_json` already defaults every optional field, so widening the persisted schema
  costs nothing. **This is a quiet trap** — nothing fails, the note just lands with the wrong
  `type:` in its frontmatter, and only a reader who looks will notice.
- **Accepting must grow a side-effect hook, and it is not a small point.** `_write` is the *whole*
  of what accepting does today. Increment 5 needs two more things at that moment — **append the
  confirmed terms to the glossary** and **delete the audio original** — and both are the answer to
  "what does *success* mean". Note where that leaves you: acceptance happens in a **poller
  background task**, turns after the handler that downloaded the file, so whatever holds the audio
  has to be reachable from there (`review.work_dir` is the obvious home — it lives exactly as long
  as the review) or the deletion must be given up on deliberately. The glossary append is a side
  effect **after the last LLM call** of the last turn, which is the one place the rule still holds.

#### The STT dependency — measured, and the trap is that it looks fine on this machine

> ✅ **All of this held**, and the trap was real: the model is cached here, so nothing in
> development could have failed. Two corrections from reading `mlx_whisper`'s source (both in
> *as built*): a *cached* model still hits the network unless you pass the resolved snapshot
> **directory**, and `ffmpeg` cannot be handed a path at all — it must go on `PATH`. One stale
> figure below: the package in meeting-transcriber's venv is v0.4.3, not v0.1.0.

`~/Projects/meeting-transcriber/` is the reference implementation: **read it, and lift what you
need.** Measured on this machine (2026-07-16):

| Thing | Where it actually is | State |
|-------|----------------------|-------|
| `transcribe.py` | `~/Projects/meeting-transcriber/.claude/skills/meeting/scripts/transcribe.py` | 6.2KB, a clean `transcribe(...)` function + CLI — **port the function, don't shell out to it** |
| `SKILL.md` | same dir | 8.9KB — the meeting-note template and steps the prompt must carry |
| `glossary.md` | same dir | **19.9KB**, shared (open decision 5: share / copy / symlink) |
| **`mlx-whisper`** (package) | meeting-transcriber's **own venv**, v0.1.0 | ❌ **not in this project's venv** → `requirements.txt` |
| **model weights** | `~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo` | ✅ cached — **1.5GB** |
| `ffmpeg` | `/opt/homebrew/bin/ffmpeg` | ✅ present — and see the PATH trap below |

**The model is an HF repo id, not a vendored file.** `transcribe.py` passes
`path_or_hf_repo="mlx-community/whisper-large-v3-turbo"`, and `mlx_whisper` **downloads it on first
use** if it is not cached. So "install the model" is not a step anyone wrote — it is a side effect
of the first transcription.

**That is the trap, and it is increment 1's PATH bug wearing a third hat: it cannot fail here.**
The 1.5GB is already cached *because the owner uses meeting-transcriber*, so on this machine the
first run will be instant and correct. On a fresh machine — or after a cache clear — the first
meeting audio would instead **stall for minutes mid-job, silently, downloading 1.5GB, and fail
outright with no network**. Development cannot catch it, exactly as a terminal-launched app could
never catch the Dock's PATH.

**So the repo must own this rather than inherit it** (the owner's call — independence and
completeness):

- **The package** goes in `requirements.txt`. It is Phase 2's one new runtime dependency, as planned.
- **The weights must be *deliberately* present, not incidentally.** Detect the cache, and make
  acquiring it an explicit, visible step rather than a stall inside someone's meeting note. The
  natural home is the **`claude-engine`-style health probe** (`core/health.py`): a probe that
  reports **DEGRADED — model not downloaded** costs nothing, is local, and turns an invisible
  multi-minute hang into a thing the owner can see and act on *before* they send a recording.
  Consider a warm-up/`--download` path too; do **not** let a 1.5GB fetch happen for the first time
  inside a job that the owner is waiting on.
- **Do not import from `~/Projects/meeting-transcriber/`.** Port the code in. That project is the
  source to read, not a runtime dependency — the bot must stand alone. (The *glossary* is the one
  genuine cross-project file, and that is open decision 5.)
- **`mlx-whisper` and `ffmpeg` both live in `/opt/homebrew/bin`-shaped places**, so both hit the
  launchd-PATH wall from the Dock (increment 1). `mlx-whisper` is imported, so it follows the venv
  rather than PATH — but **`ffmpeg` is a subprocess and will not be found**. Resolve it the way
  `engine/claude_cli.resolve_executable` does. This is the *third* time this exact bug will have
  been available to us; it is the one increment-1 lesson that keeps paying.

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
- **`engine/claude_cli.resolve_executable`** is the PATH-resolution pattern `ffmpeg` needs — see
  the STT section above, where this bug is now measured rather than predicted.

**The decisions increment 5 has to make (each one is genuinely open):**

> ✅ **All settled before any code was written — see *The decisions, settled* below.**

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

### Increment 5 (as built) — audio → meeting note

Delivered: `stt/whisper.py` (the port) + `scripts/download_model.py`, `files/glossary.py`,
`handlers/audio_handler.py` (the producer), `engine/prompts/meeting_note.md` +
`meeting_glossary.md`, the queue in `core/session_store.py` (`ReviewState.QUEUED`, `note_type`,
`tags`), `conversation.activate` / `promote_next` / `_accept`, the `whisper-stt` and `glossary`
health probes, five settings, the vault's **filename convention** across every route, and the
**deletion of the `#검토` scaffolding**, and the **window** the new probes broke. Suite: 474 → **636**
(it peaked at 601 and *drops* here, because the scaffolding's ~28 tests went with their subject).

Since shipped, one owner-found fix: **every note was written in UTC** — `localtime.py`,
`files/audio_meta.py`, `NOTE_TIMEZONE`, and the conversion at `route()`. See *The post-increment fix*
below; it is the only thing in Phase 2 that was wrong on **every** route rather than one.

**The decisions above all held.** What follows is what building it changed or found.

#### Three things the vault knew and this document did not

1. **`type: meeting-note`, not `type: meeting`.** The handoff above says a meeting note wants
   `type: meeting`. The vault has **35 notes with `type: meeting-note` and zero with `meeting`** —
   it was written from memory, and following it would have filed every meeting note into a category
   of one, invisible to whatever the owner filters on. **Nothing would have failed**: the note lands
   with the wrong `type:` and only a reader who looks ever notices. This is increment 3's
   `.metadata.json` lesson repeating exactly — *read the consumer's source* — and the third time in
   Phase 2 that reading it beat guessing.
2. **The section headings really are English** (`## Overview` / `## Summary` / `## Discussion
   Points`), Korean content underneath, confirming `SKILL.md`'s template against real notes.
3. **The filename is `YYMMDD-<분류>-<topic>`**, and the bot's own `YYMMDD-HHMM-` appears in the vault
   **zero** times — see *The filename convention* below.

#### The audio does **not** go in the work dir (a deviation from the handoff)

The handoff says "stage the audio and the transcript in `review.work_dir`, alone". Only the
transcript does. `work_dir` is the model's entire view of the filesystem, and the model **cannot
hear** — an `.m4a` sitting there is a file `Read` hands back as raw bytes, which is precisely
increment 3's `.heic` trap (describe the header, report success). It lives in `review.audio_dir`
(`state/reviews/<id>/audio/`) instead: inside the review's tree, beside the draft, outside the cwd.
Same lifetime, none of the exposure.

#### `mlx_whisper`'s two contracts, read off its source rather than assumed

- **A cached model still hits the network.** `load_model` only skips `snapshot_download` when
  `Path(path_or_hf_repo).exists()` — and `snapshot_download` phones HuggingFace to resolve `main`
  **even when the model is fully cached** (measured: three requests to huggingface.co on a cached
  transcription). So `model_is_cached()` passing and then passing the *repo id* anyway would have
  left exactly the offline failure the check exists to prevent. `transcribe` passes the **resolved
  snapshot directory**, which takes that branch out entirely: no round-trip, and it transcribes
  under `HF_HUB_OFFLINE=1`. It also halved wall time on a short clip (9.6s → 4.4s).
- **`ffmpeg` cannot be handed a path.** `mlx_whisper.audio.load_audio` builds its command as the
  bare string `["ffmpeg", "-nostdin", …]`. So the increment-1 lesson ("resolve it, don't assume
  PATH") is *necessary but not sufficient* here: the resolved directory has to be **prepended to
  `PATH`** before `mlx_whisper` shells out, or a Dock-launched app cannot decode audio at all.

#### Two bugs only the real runs found — both silent, as increment 4 predicted

The handoff's warning ("increment 4's four bugs were all silent failures") landed. Both of these
save a note, raise nothing, and leave the suite green.

1. **A preamble before the `제목:` line.** `parse_draft` looks for the title on the **first** line.
   The first real run obeyed "no preamble"; the second opened with `글로시리 확인이 완료됐습니다. …`
   and a `---`. That one slip cost everything at once: the title fell back to the filename
   (`meeting`), the tags came out empty, and the preamble *plus the raw `제목:`/`태그:` lines* were
   saved as the note's body. **A flaky instruction needs a structural fix** (increment 2's PDF
   lesson): the template now says the reply's first characters are `제목:` *and* explains why (it is
   parsed, not read), and `strip_preamble` — inside `parse_draft`, bounded to 8 lines so a `제목:`
   deep in a real body cannot eat the note above it — makes the parsing stop depending on it.
2. **A trailing `---`.** The model put a rule between the note and the questions heading;
   `parse_draft` splits on the heading, so it stayed in the body and the note ended in a dangling
   `<hr>`. `strip_trailing_rule` handles it.

Both live in the **shared** path, so the revise turn gets them too — which matters more than it
looks: the real correction run proved the revise turn *does* re-emit the `태그:` line that
`split_tags` exists to catch. Had that been producer-only, the first correction to any meeting note
would have written `태그: …` into the note body.

#### Three more from reviewing the diff and looking at the app — the trick increment 4 got three from

None is reachable from a test that was not written for it, and all three are silent.

1. **The promotion race, and it is entirely plausible.** A producer creates its entry `QUEUED` and
   *then* spends minutes drafting — so the queue also holds work **still in flight**. If the owner
   answers the open review during that window (reading it on their phone while the second recording
   is still transcribing — i.e. exactly what happens when two recordings arrive together), the
   review ends, promotion fires against the unfinished one, and **both halves lose**: the owner gets
   `📝 초안이 준비됐습니다` with an *empty body* (the draft file does not exist yet), and the producer
   then writes its own object back over the activation — leaving a finished draft `QUEUED` with
   nothing pending, so the poller stops and nobody is asked until the next restart. `promote_next`
   now promotes only a review whose **draft file exists**, which is the same signal
   `discard_incomplete` already trusts: only a completed turn creates it.
2. **A crash mid-download poisons the transcript cache.** `_download` reused whatever was in the
   staging dir. A `DeferMessage` leaves a *finished* download and its transcript — the state the
   cache is for — but a **crash** leaves a truncated `.m4a` and no transcript, and ffmpeg will
   happily decode the valid prefix of one. The replay would transcribe half a meeting into a
   confident, complete-looking note. The staging dir is now trusted **only** when the transcript is
   there beside the audio; anything else is wiped and re-fetched.

3. **The health panel clipped the probe that exists to be read.** Found by *screenshotting the real
   window*, not by a test — the suite was green throughout. The panel reserved a flat 96px ("room
   for the 4 probe lines") and the window declared `setMinimumSize(420, 620)`. Seven probes need
   768px, and a fixed minimum *below* what the layout needs makes Qt squeeze the cards past their
   own minimums and clip: **`whisper-stt` and `glossary` were invisible**, `claude-engine` cut off
   mid-path. (A word-wrapped `QLabel` also never tells a layout how tall it is, so the long paths
   ate the lines below them.) This is the worst one of the three, because the whisper-stt probe's
   *entire purpose* is to say "model not downloaded" **before** the owner sends a recording — and
   its message is the longest line the panel ever shows, so it was the first to vanish. **A probe
   you cannot read is the failure it exists to prevent.** Fixed by deleting the promise rather than
   raising it: the panel's height is derived from its text, and the window keeps only a minimum
   *width* so the layout's own `minimumSizeHint` is the floor. A bigger constant would just postpone
   it — which is precisely how the 96 got there.

All three are the increment's own machinery producing the failure it exists to prevent — which is
what increment 4 said about its own three (*"silent strandings of exactly the kind the state machine
exists to prevent, reintroduced by the machinery meant to prevent them"*). That is now **eight bugs
across two increments** found by reading the diff or looking at the app, rather than by running the
suite. Budget for it. **And note what caught the third: a screenshot.** Every probe added to
`health.py` is a line in a fixed-size panel, and nothing in the suite had ever looked at it.

#### The window — where adding two probes led, and it was further than expected

The clipping above was the first pull on a thread. `health.py` gained two probes; the window had
been built for four; and the owner then reviewed the surface rather than just the bug. The result is
a **compact status bar** — one row (round start/stop button, a line of text, a health light) that
expands to the HEALTH and ACTIVITY cards on demand. **440×72, from 533×620.**

`ui/status_widget.py` is gone: it drew a big status "face" that *pulsed on a loop while the bot
worked* — the app blinked at the owner for as long as it was doing its job — and the face, a bold
status name and a tagline were three ways of saying one thing. With the face removed it was a label
with a fallback rule, so the window took that over. `elided_label.py` and `icon_buttons.py` are new.

**Four more bugs, and their shape is worth carrying forward, because none of them raised.**

1. **A constant overriding what the layout already knew — three times.** `setMinimumSize(420, 620)`
   when the panel needed 768; then `setMinimumWidth(460)` when the row needed 592; and
   `ElidedLabel` forcing `Ignored` horizontally, which a *stretchy* label needs but a caller's
   `setFixedWidth` cannot survive — `setFixedWidth` pins min/max but not the policy, so the layout
   sized the health column as zero-width, gave its space away, and drew the label at its real 150px
   over the neighbour it had just placed. **A minimum below what the content needs does not shrink
   the window; it lets Qt squeeze the children past their own minimums and clip them.** The window
   now sets no explicit minimum in either direction.
2. **Health was asked before the bot existed.** `start_bot` only *queues* `start()` on the loop, and
   the click asked for health on the very next line — so the check ran against an unconnected client
   and answered `telethon-auth: not logged in (run login.py)`. Red, alarming, and false on a machine
   that is logged in fine, until the 15s timer happened to re-check. The worker asks when the
   starting sequence resolves, which is the first moment a check can tell the truth.
3. **…and kept asserting health after it stopped.** A green light on a stopped bot is a claim nobody
   is standing behind, and it would go on making it all night while the CLI's login quietly expired.
   Grey on stop — plus dropping a check that was already in flight, which otherwise lit the bot back
   up green a moment after it was told to stop.
4. **Two glyphs pretending to be one shape.** `⌄` and `⌃` are different characters (U+2304, U+2303):
   different weights, sizes and baselines, so the chevron changed shape and jumped as the panel
   opened — which is what read as "misaligned". Both buttons are painted now, the chevron drawn once
   and the painter rotated 180°, so up and down are one shape by construction. **A font is not a
   shape library**, and the text-presentation variation selector `▶︎` had needed to stop macOS
   rendering it as colour emoji was that bug admitting itself.

(2) and (3) are the same bug from opposite sides — *invent a failure that is not there* and *assert
a success you have stopped checking* — and both defeat the panel's entire purpose. **A probe is only
worth reading if it is green exactly when green is true.**

`test_ui_smoke.py` went from 3 tests to 27, and most of what it now asserts is *layout*, which is
unusual and earned. It also turned up a green suite that was partly green because it was not there:
**seven test names were duplicated**, and Python keeps only the last definition, so the earlier
copies had silently never run. They came from appending blocks to the file instead of editing it.

#### The filename convention — the vault's, and the third time reading it beat guessing

Every note is now `YYMMDD-<분류>-<topic>.md`, matching the vault. **The bot's own `YYMMDD-HHMM-`
convention does not appear in the vault even once**, so there was never anything to stay consistent
with — only a convention to join. Of the 181 notes there, 103 carry a two-syllable Korean category:
회의 (41), 전략 (40), 조사 (12), 보고 (3), 안건 (2), and one each of 초안/의견/배경/기획.

It is a **filename slot, not a frontmatter tag** — the vault's meeting notes carry tags like
`에이닷`/`B2B` and never `회의`. Don't write it in both.

| Route | 분류 | Decided by |
|-------|------|-----------|
| audio (meeting note **and** the transcript-only fallback) | `회의` | the handler — a recording is a meeting whether or not the note got made |
| text, image | `노트` | the handler — the owner's call: a message or a screenshot is a note |
| `.txt` / `.csv` | one of the six | `text_enrich`'s JSON gained a `category` field |
| `.pdf` | one of the six | `pdf_to_markdown` gained a `분류:` first line |
| `.md` passthrough | `노트` | nothing read it — that route never calls the engine (increment 2) |

**The document category rides a call that already happens**, in both cases: classifying a document
is not worth a second round trip when a model is already reading it. Two guards make that safe:

- **The set is closed** (`전략 기획 조사 안건 보고 초안`) and anything outside it becomes `노트`.
  The category is interpolated into a *filename*, so an open set would let a model's improvisation —
  or a `/` — name a file. `노트` is the honest answer for "we could not tell".
- **`normalize_category` NFC-normalizes first**, and that is not paranoia: the vault already holds
  one `전략` written as **decomposed jamo (NFD)** beside 40 composed ones. "The model said 전략" and
  "the string equals 전략" are not the same question, and without this an NFD answer would silently
  fall back to `노트` with nothing on screen to explain why.

The `분류:` line is stripped before the note is written — the same shape as the `태그:` line, and the
same failure if it isn't (it becomes the document's first line). A conversion that *forgets* the line
is not a failed conversion: it falls back to `노트` rather than costing the owner a note the model
did produce. **Driven for real**: a strategy PDF → `260716-전략-…`, an agenda `.txt` →
`260716-안건-…`, with no leak into either body.

`build_filename` takes `category` with **no default**, deliberately: it is a claim about what the
note *is*, and every route knows its own answer — a default would let a new route silently inherit
someone else's. Dropping `HHMM` also means two notes in one day can now collide, which `unique_path`
already handled.

#### Points worth knowing before touching this

- **`store.update` no longer overwrites the list.** It was `data["reviews"] = [review]`, which was
  correct while exactly one review could exist and is a silent eraser with a queue: saving the
  active review would drop every queued one. `_put` replaces by `message_id`.
- **`create()` always makes a review `QUEUED`**, and the producer activates it once a draft exists.
  Not bookkeeping: a review created answerable is visible to `pending()` for the whole drafting turn
  — *minutes* for audio — so a reply arriving meanwhile would be routed into a review with no draft.
- **Promotion is wired at every place a review can end** (a reply, the expiry tick, startup), and
  **before `_on_review_reply` returns** — the poll loop re-checks `is_active` the moment it does.
  Forgetting one leaves a finished draft nobody is ever asked about, and nothing polls for a review
  nobody has been asked about. Startup needs it too: if the review ahead of a queued one ended just
  before the app closed, no other trigger exists.
- **The transcript cache is `state/audio/<message_id>/`** and the deferral path is the *only* one
  that does not delete it. Keyed by message id because Telegram media is immutable.
- **`_accept` is the one place a side effect follows an LLM call at the *end* of a review** rather
  than the start of a job. Order: glossary turn → note → glossary append → `store.remove` (which
  takes the audio). A failed glossary turn **still writes the note**: they said 확인.
- **A non-meeting review makes no glossary call**, and the branch is not dead weight — it is what
  open decision 6's producer (a text review started from the bot DM) will need.

#### Owner verification — and the two things only a real meeting could show

The owner ran a **real ~40-minute meeting** through it (`을지로2가.m4a`, 26MB → 878 segments, a
38KB transcript) and it worked end-to-end: draft → correction → `확인` →
`260716-회의-하반기_모두의_ai_전략_덱_검토.md`. The measured turns:

| Turn | Time | Est. |
|------|------|------|
| Whisper (local, free) | 320s | — |
| **Draft (turn 1)** | **423s, 3 turns** | ~$0.56 |
| Revise | 82s | ~$0.42 |
| Glossary | 1.7s | ~$0.05 |

**Two problems fell out, and neither was findable without a real recording of a real length.**

1. **The 600s timeout was a guess, and it was wrong.** The draft turn ate **70% of it** — and the
   owner's follow-up is what sized the fix: *"1시간이 넘는 회의도 종종 있다"*. That reframes the
   measurement, because 24,215 characters of Korean speech is already ~an hour, so **423s was the
   ordinary case, not the bad one**. A two-hour meeting would blow 600s, and blowing it does not
   delay — it **destroys the note**: `ClaudeTimeout` degrades to a transcript-only note, so minutes
   of Whisper and a full drafting pass are spent and thrown away at the last step. The asymmetry is
   total (too long costs patience nobody is spending — the job is async and unwatched), so it is now
   **`CLAUDE_MEETING_TIMEOUT_SEC`, default 3600s**: a **hang detector, not a budget**. An hour means
   *this is stuck*, not *this is a long meeting*. Whisper is not timed at all, for the same reason.

   It gets **its own dial rather than a floor over `CLAUDE_TIMEOUT_SEC`**, because how long a
   meeting takes to write up has nothing to do with how long a text memo may take — and because the
   owner says the variance is real, which is exactly what a setting is for.
2. **13 minutes of total silence**, from sending the recording to the review block. Every other
   pipeline replies in seconds, so this is audio's alone — and the owner, on a phone with no view of
   the app's PROCESSING status, reasonably concluded the bot was broken and said so. The client now
   **acknowledges a recording before the slow work starts**. It lives in `ClientService` rather than
   the handler because the client owns the notifier and already decides everything else the owner
   hears; it is the one safe pre-LLM "side effect", because a replayed duplicate of "got it" is
   noise rather than damage.

**Cost, now measured rather than extrapolated.** One reviewed meeting is **~$1.03 est** (0.56 draft
+ 0.42 revise + 0.05 glossary), and **every additional correction is another ~$0.4** — the revise
turn re-reads the whole transcript. That is 10-30x a text memo, and the *Cost note* section's
~$0.03-0.09 figure does not describe this pipeline at all. On a Pro plan whose 5-hour window is
shared with the owner's own Claude Code sessions, a few meetings is a real fraction of the
allowance.

*(Two notes on that run. **The transcript**: two of the 878 segments were Whisper repetition loops
— `우리 우리 우리…` ×70 over silence. 0.2%, and the model drafted through them without trouble, so it
is recorded rather than fixed; if it worsens, the lever is `condition_on_previous_text=False`, which
is what feeds those loops. **The glossary added nothing**, which looked like a miss and was not —
the owner had told the model during the review not to update it, and it obeyed. Worth knowing that
the failure mode of this feature is silent in both directions: a glossary that does not grow may be
correct, and only the owner knows which.)*

**Verification status.** Also driven for real, end-to-end through `handle_audio` and `handle_reply`
against the real `claude` binary and real `mlx-whisper`, on a synthesized Korean recording:

- **The glossary substitution works, and it is the point of the whole feature.** Whisper heard 티맵
  as **"팀웹"** — a plausible Korean word that is not the product's name, in a sentence that reads
  perfectly. The glossary's row 3 already carries `팀앱, 팀웨이, 팀웹 → T-map`; the note says T-map
  throughout and the model **did not ask about it**, because it is settled. It flagged
  「맞고」→「맡고」 instead — a second real STT error nobody planted.
- **The full loop**: draft (title + tags + 4 timestamped questions) → a correction (`김철수가 아니라
  김철승 책임입니다`) → applied throughout, tags updated → `확인` → note written with
  `type: meeting-note`, `reviewed: true`, and the glossary grew
  `| 김철수 | 김철승 | 인명 | SKT 책임 |` — **keyed on the wrong spelling, valued at the corrected
  one**, inserted inside the table. The audio and the review tree are gone.
- **The isolation was asserted as the model saw it**: `work_dir` listing = `['transcript.txt']`.
- The tilde rule survived (`10-20%`), as it did in increment 4.

*(The probe appends to a **copy** of the glossary; the owner's real file was never touched.)*

#### The decisions, settled — recorded before any code was written

*(All five, in the order the handoff above poses them. The "as built" section is what came of them.)*

**1. One at a time → a queue, and what is queued is the *asking*, not the job.**

Addressing was rejected on ergonomics that decide it: a handle on every reply taxes the **common**
case (one review, a bare `확인` from a phone) to fix the rare one, and a *forgotten* handle puts us
straight back to guessing — the exact bug the one-at-a-time constraint exists to prevent. It moves
the ambiguity onto the owner's typing discipline rather than removing it.

The queue keeps the expensive work where it already is: `handle_audio` **always** downloads,
transcribes, and runs turn 1, so the transcript, the session, and the draft all exist before the
queue is consulted. Only then, if a review is already being asked about, the new review parks in a
`QUEUED` state instead of being sent. **Promotion is therefore `created_at = now` plus one DM — it
makes no LLM call and cannot fail.** That is what settles the design: promoting *by drafting later*
would run an LLM turn from a background task with no handler to replay, needing its own retry
driver, its own give-up counter, and its own answer for a usage limit — a second state machine
beside the one increment 4 already built.

Exactly one review is ever *asked about*, so a bare `확인` stays attributable and increment 4's
constraint is **preserved rather than weakened**. And the clock rule falls out for free: `created_at`
is stamped at promotion, which is precisely "the clock starts when the owner is asked".

**2. Replay cost → cache the transcript, keyed by message id.** `state/audio/<message_id>/` holds
the download and the transcript; each step is skipped when its output is already there. A
`DeferMessage` **leaves the directory** — it *is* the cache — and every other exit deletes it. The
key is sound because a Telegram message's media is immutable: the transcript of message N is a pure
function of N. This costs ~15 lines and removes the one unbounded rework in the pipeline, because
`DeferMessage` can only fire at turn 1, i.e. **after** Whisper has already run (a review turn never
defers — increment 4). That is exactly the case the cache exists for.

**3. Glossary git commit (open decision 2) → no. Append only.** Two reasons, and the second makes it
moot. A `git add && commit && push` from a **poller background task** fails silently the moment
credentials, network, or a conflict go wrong — and every one of increment 4's four bugs was a silent
failure. More decisively: decision 4 puts the glossary **inside the vault**, which the owner already
backs up wholesale to a private repo. The backup is the owner's existing routine; the bot has no
business owning a step that is already owned, and `/meeting`'s own step 8-1 still commits it when
they use that.

**4. Glossary location (open decision 5) → the vault, at
`~/Documents/MarkNotes/.claude/contextbot/glossary.md`.** **Owner's call**, and the reasoning is
theirs: committing it into *this* repo would publish ~20KB of real names (SKT/Meta executives,
internal product codenames) to `github.com/jhleebre/telegram-bot`; not committing it anywhere would
leave the accumulated corrections unbacked-up; and the vault is already private, already backed up
in full, and already the bot's output target. `GLOSSARY_PATH` overrides it.

**Two facts were checked rather than assumed, and both had to hold or the choice defeats itself:**

- **The vault's git tracks `.claude/`** (29 files) and its remote is `marknotes-data.git`, private.
  So the glossary rides the existing vault backup. Had `.claude/` been ignored there, this location
  would have quietly delivered the *opposite* of the backup it was chosen for.
- **MarkNotes skips every entry starting with `.`** — `fileOperations.ts:16` (the file tree) and
  `searchService.ts:80` (search). So the glossary is not a note, is not searchable, and never
  appears in the vault UI. The owner's first instinct, `4_archive/4_glossary/`, would have made it
  all three: a 20KB table of names sitting in the vault as a document.

It lives under `.claude/contextbot/` rather than `.claude/skills/meeting/` because the vault's
`skills/*` are **real skills the owner invokes**, and this is the bot's data parked in the vault —
naming it so keeps it from advertising a skill that does not exist. **Migration done:** copied, not
moved, so meeting-transcriber's `/meeting` keeps reading its own. A missing glossary is **not an
error** — no substitutions, the note is still made, and the health probe says so.

**5. Accept-time side effects → the review directory owns the audio; the glossary entries come from
one resumed turn.** These are the two things "success" now means, and they land in a poller
background task turns away from the handler that downloaded the file.

- **The audio deletion is arranged rather than remembered.** The audio is staged in
  `state/reviews/<id>/audio/` — inside the review's tree but **outside `work_dir`**, beside the
  draft, so the model's view stays the transcript alone (increments 2-3's isolation rule still
  binds). `store.remove()` already `rmtree`s that tree, so *deleting the audio is already what
  ending a review does* — on accept, on cancel, on delivery, on expiry, with no new code and nothing
  to forget. A deletion that someone has to remember is a deletion that leaks.
- **The glossary rows are asked for at accept, on the resumed session.** Which corrections were
  *confirmed* is knowable only from the conversation — the model proposed the terms, the owner
  accepted some and corrected others. So accepting resumes the session once and asks for the
  confirmed rows. Order: **LLM call → write the note → append the glossary → `store.remove()`** (the
  audio dies there). Every side effect after the last LLM call, which is the one place that rule
  still applies.
- **Rejected: storing turn 1's proposed rows and appending those on accept.** A proposal the owner
  *corrected* would then be appended as though confirmed, and a wrong glossary entry silently
  mis-corrects every future meeting note. That is strictly worse than having no glossary at all —
  it is the increment 2/3 fabrication trap with a persistence layer.
- **It is best-effort by construction.** If the glossary turn fails (usage limit, lost session), the
  **note is still written** and the reply says the glossary was skipped. `확인` must never cost the
  owner the note they just approved.

#### The post-increment fix: every note was written in UTC (owner-found, in real use)

The first real meeting note came back with an Overview table nine hours off, and the cause was one
that no test could have failed on: Telethon hands us `message.date` as UTC-aware, and every render
site formatted it as-is. `strftime` on a UTC datetime prints the *UTC* wall clock — a perfectly
well-formed timestamp that is simply not the one anybody read. **UTC was never wrong about the
instant; it was answering a question nobody asked.** A datetime carries two separable facts — which
instant, and whose clock — and the code only ever handled the first.

**It was never only the meeting note.** The same value named the file and filled the frontmatter, so
the fix had to be wider than the report:

| Surface | Was | Now |
|---|---|---|
| `meeting_note.md`'s `\| Date \|` | the UTC wall clock — **what the owner saw** | the meeting's own clock |
| `YYMMDD-` filename | the UTC date — a note captured **before 09:00 KST filed under yesterday** | the local date |
| frontmatter `date:` | `+00:00` | the local offset |

The middle row is the one worth keeping in mind: **nobody reported it, and nobody would have.** A
note filed one day early is not visibly broken — it is a note, on a plausible date, and only a reader
who goes looking for a specific morning ever finds out. It shipped in increments 1–5 and rode along
under a bug that was only noticed because the *meeting* route prints the clock where a human reads it.

**The conversion happens in `route()`, and that placement is the point.** It is the one gate every
handler passes through, so no handler — including one written next year — can forget it, and the
failure mode if it did is a confidently-wrong note that nothing downstream can catch. Same reasoning
as the audio deletion above: an obligation someone has to remember is one that leaks. `as_local` is
idempotent so the audio pipeline can re-derive its own answer on top without the two fighting.

**`NOTE_TIMEZONE`, not a hardcoded `+09:00`.** A fixed offset would be the same class of bug wearing
a different hat — wrong in a zone with DST, wrong when the owner travels. An unknown zone name
**refuses to start** rather than falling back to Seoul: the symptom of a silently wrong zone is
exactly the confidently-wrong note this setting exists to end.

##### The recording's own metadata beats the send time — where it exists, which is rarely

The owner asked for the better answer: date a meeting by where it was *recorded*, not where it was
uploaded from. This is real — `message.date` is the **upload** time, so a meeting recorded at 14:30
and sent that evening was dated by the evening even once the zone was right — but the ceiling on it
is set by what actually survives in a container, and that had to be measured rather than assumed:

- **`com.apple.quicktime.creationdate` → `2026-07-17T14:30:00+0900`.** The only tag that answers the
  question. The offset is the recording device's own, so it carries *both* the instant and whose
  clock — which is what "where was this recorded" reduces to in a form that survives a file transfer.
  It lives in the QuickTime `mdta` box, so a re-encoder **drops** it rather than inventing one: its
  presence is evidence, not decoration.
- **`creation_time` → rejected, and this is the load-bearing one.** It is the obvious tag to reach
  for and it is a trap. The MP4 spec defines it as UTC and ffprobe normalizes it to `Z`, so it
  repeats the instant Telegram already gave us and knows nothing about where. Worse, **every encoder
  stamps it** — ffmpeg writes the transcode time by default — so on a re-encoded file it is a
  confident lie about the recording time. Reading it would trade a knowably-wrong date for an
  unknowably-wrong one, which is the `.heic`/`.docx` fabrication lesson in date form.
- **ID3 `TDRC` → rejected.** No offset, and ID3v2.4 says UTC while half the world writes local time
  into it. Unresolvable.
- **GPS (`location.ISO6709`) → rejected.** Coordinates need a lat/lon→zone lookup (`timezonefinder`
  plus map data) to become an offset, and Apple writes it *alongside* `creationdate`, which already
  carries the answer outright. A real dependency for no gain.

So: **an explicit UTC offset or nothing**, and the metadata time is used **as-is, never re-converted**
— forcing a Berlin meeting onto Seoul's clock would discard the one fact it came for.

**Nothing is the common answer, by a wide margin.** A Telegram *voice message* is re-encoded to Opus
and arrives with **no tags whatsoever** (measured). This pays off only for a recording sent as a
**file** — which is how a real meeting arrives anyway, since voice messages are memos. The fallback
is the path most meetings take, so it is not a corner case and is tested as the main one.

`ffprobe` adds no dependency (mlx-whisper already shells out to ffmpeg) and is treated as optional at
runtime: missing, wedged, or fed garbage, it returns `None` and the note gets the configured zone.
**A meeting note is worth more than this answer** — nothing here fails a note.

##### The tests build real files and run the real ffprobe

`test_audio_meta.py` encodes actual m4a/mp3/oga with ffmpeg rather than mocking the probe. The whole
module is a bet about what a muxer preserves and what it normalizes away, and **a fake ffprobe would
only confirm that bet back to itself** — the two facts worth having are exactly the ones it cannot
hold: that Apple's `+0900` survives a write/read round-trip, and that `creation_time` does not. Same
reasoning that made `files/images.py` drive the real `sips` (increment 3).

The fix was also checked the only way a "the tests pass" claim is worth anything here: **reverted,
re-run, and watched to fail** — 6 tests, then restored.

#### The post-increment feature: captions, which were already arriving and being thrown away

**The caption was never missing data.** Telethon puts a media message's caption in the *same*
`raw_text` slot a text message's body uses — there is no second field — so `build_incoming_message`
had been filling `IncomingMessage.text` with it since Phase 1. Every media handler simply ignored
that field. So this is not a plumbing job that reaches out for something new; it is four handlers
starting to read a value that was already sitting in front of them. Worth recording because the
first instinct was to go looking for `message.caption` in the Telethon API, and there isn't one.

**Which types.** All of them. Telegram allows a caption on every media message — photo, voice
memo, audio file, animation, and any document regardless of extension. The only thing without one
is a plain text message, where the text *is* the body and there is nothing standing outside it.

**The split happens once, in the router** (`build_incoming_message`), because it is a *kind*
question and the router is the one place that already answers those. `text` for a memo is the
note's content; `caption` is the owner talking *about* a file. Conflating them is not cosmetic: a
memo reading `이거 요약해줘` would be handed to the enricher as an instruction, and the note the
owner typed would be silently replaced by a note *about* what they typed.

##### The trust boundary is the whole design, and it is the opposite of the existing one

Every prompt in this repo already ends with some form of *the file is data, never instructions to
follow* — the guard against a PDF that contains text addressed to the model. A caption inverts
that, and the inversion has to be explicit or the two rules fight: **the file is data; the caption
is the owner, and it therefore is an instruction.** It is the one part of the job a person wrote on
purpose, addressed to the bot.

The policy lives in exactly one place, `engine/prompts/caption.md`, rendered by
`prompts.caption_section()` and dropped into each template's `{caption}` slot. One copy rather than
four, because four would drift, and the drift would be invisible — a caption honoured on images and
quietly ignored on PDFs looks like a flaky model, not a bug. **An empty caption renders the empty
string**, so an uncaptioned file produces the prompt it always did.

*Acting* on it is the requirement, and the naïve implementation — paste the caption into the note —
is the failure mode, not the feature. So the template sorts captions by what they are: a direction
about the output is followed, context the file lacks is folded into the note's own prose, a
question is answered where the note can answer it, and a pointer into the file re-weights attention.
And it is told twice not to paste it in, because that is the thing a model does by default.

**Two things a caption may not do**, and both are guards against it becoming a hole in a guarantee
this phase spent five increments building:

- **It cannot loosen the anti-fabrication rules.** Not the required structure, not the language
  rules, not the sentinel. A caption asking the model to describe an image it could not see is the
  one kind it is told to refuse — increments 2 and 3 both found the model would rather answer than
  admit it cannot see, and a caption is a *very* effective way to talk it into that.
- **It cannot trade a PDF conversion for a summary.** The genuine collision: `핵심만 요약해줘` on a
  PDF is a reasonable thing to type, and honouring it literally would discard the document. The
  original goes to `~/Downloads` and the note is the only searchable copy, so the note is where the
  document has to survive. Resolution: the summary is **added** as a `## 요약` section above the
  full transcription, never substituted for it.

##### Acting on a caption spends it, so the raw text is kept in frontmatter

A caption that worked correctly has disappeared — it went into the description's wording, the
Overview row, the tags. That is the point, and it is also lossy: nothing on the note would explain
why it reads the way it does. So the owner's exact words are written to `caption:` in the
frontmatter, and the body is left free of them.

For audio this is the only reason the value is **persisted on `PendingReview`**. The producer sees
the caption; the note is written by `conversation._write` at the far end of a review that may be
days and a process restart later. Widening the record cost nothing, as increment 4 designed for —
every optional field defaults, so a review already in flight still loads with `caption: ""`.

**One route ignores captions on purpose: a sent `.md`.** That route runs no model — the file *is*
the note, saved byte-for-byte — so there is nothing to act *with*. The alternatives are rewriting
the owner's own file, which is the thing that route exists not to do, or appending their words to
it, which is the verbatim paste every other route is told to avoid. Ignoring it is the honest
answer, and it is written down at the function rather than left to be rediscovered.

##### The degraded paths get a title out of it, which is where it is worth most

`_title_for` (image) and `_fallback_title` (audio) now try the caption's first line before the
filename. These are reached only when **no model ran** — Claude disabled, or the draft failed — so
nothing looked at the content and the alternative is `IMG_4821` or the bare word `이미지`. Even a
caption phrased as an instruction beats those: `영수증 정리해줘` is a worse title than the model
would have written and a far better one than `IMG_4821`, because it is the only thing on the note
that says what the capture was.

##### Testing

`test_captions.py` (27) walks the seam and every route past it: that all six media kinds carry a
caption and a text message carries none, that the shared block renders nothing when empty and
frames the text as the owner speaking when not, that **braces in a caption are inert** (it is a
substituted *value*, never itself formatted — otherwise a caption mentioning `{path}` would either
raise `KeyError` or interpolate another placeholder into the owner's words), that each of the four
templates has a slot, and that the PDF and meeting rules above are actually in the prompts.

The handler assertions are deliberately shaped as *reaches the prompt* and *survives in
frontmatter*, never *appears in the body* — asserting the body would pin the exact opposite of the
behaviour. The audio case is one end-to-end test rather than three unit ones, because the chain
(caption → prompt → persisted review → note frontmatter) crosses two modules and a process
boundary, and any link failing would leave the caption stopping silently at the draft.

Suite: 689 → **716**.

#### The post-increment feature: the idle status reports what is left of the plan

**The allowance was the one number the bot knew about and never said.** Every pipeline here spends
from the subscription's two rolling windows — 5 hours and 7 days — and the whole app is already
built around hitting them: capture defers on `ClaudeUsageLimit` and halts, a review that stalls
mid-revision stays open and waits for the reset, and the *Cost note* below is an argument about
exactly this budget. The owner could see none of it from Telegram, which is where they are standing
when they hand the bot work.

So the idle status carries it, and the trailing half of that message paid for the room — the two
lines explaining what the bot DM is for had been read long ago and were costing space in a reply
generated by "answer every message, without fail":

```
🤖 실행 중입니다 — 지금은 검토 중인 초안이 없습니다.

메모·파일·녹음은 Saved Messages로 보내주세요.

📊 사용량 (이 기기 기준)
• 5시간 한도: 31% 사용 — 7월 20일 오후 2:49 리셋
• 7일 한도: 5% 사용 — 7월 24일 오후 7:59 리셋
```

##### `/usage` is a local command, which is the only reason this is affordable

`claude -p /usage` reads this machine's own session records and prints them: no model call, no
usage spent, ~2s measured against v2.1.187. A status reply that cost a model turn to say "nothing
is happening" would be absurd, and the feature would not exist.

It cannot go through `ClaudeCLI.run()`, and not by accident — `_build_argv` passes
`--disable-slash-commands`, which is right for every prompt this app sends a model and exactly
wrong here, because `/usage` **is** the slash command. Nor is `--output-format json` used: the
local commands answer in prose. So it is a sibling of `check_auth` (`ClaudeCLI.usage_text()`),
which is the shape the class already had for "a local call that consumes nothing", and parsing
lives apart from it in `engine/usage.py`.

##### Reading someone else's prose, narrowly and on purpose

Three lines are matched by name and nothing else is:

```
Current session: 27% used · resets Jul 20 at 2:50pm (Asia/Seoul)
Current week (all models): 5% used · resets Jul 24 at 8pm (Asia/Seoul)
Current week (Fable): 0% used
```

**The breakdown underneath is not a limit and must never be read as one.** `/usage` also prints a
"what's contributing" report full of percentages (`82% of your usage was at >150k context`) which
the CLI itself labels approximate and machine-local, and which is free to be reordered in any
release. Matching a percentage *anywhere* in the output would start reporting that as an allowance
the day the wording moves. The two `Current week (` lines are told apart the same way, with a
negative lookahead: reading the per-model line as the 7-day limit would put a number in front of
the owner that is not the limit they are near.

**Everything degrades toward showing what was understood.** A window with no reset clause still
reports its percentage (the per-model line ships without one); a reset stamp whose wording the
pattern has never seen is passed through in the CLI's own words rather than dropped; a report where
only one window parsed shows that one. `12:05am` and `12:05pm` are covered because 12-hour noon and
midnight are where an `% 12` conversion is wrong in the direction nobody notices.

**A per-model weekly line is shown only once it has started.** It sits at 0% for anyone not using
that model, and a permanent `0% 사용` row is noise in a message read at a glance.

##### It never costs the owner the reply it rides on

`usage_summary()` does not raise. A missing CLI, a logged-out one, a timeout, or output it cannot
read all become **one line saying so** — not silence, because silence would leave an owner near
their limit and an owner with a broken CLI looking at the identical message. The status reply is
the deliverable; the usage block is a footnote on it, and the whole failure surface is subordinate
to that ordering. This is the same rule the glossary turn follows at the end of a review.

**It rides on the idle reply only.** While a review is open the message's job is to name the draft
that is waiting and the two words that answer it, and a limits table under that buries the question
the owner still has to answer — so `client_service` reads usage only when `store.pending()` is
None, which also keeps the 2s off the path where it is pure delay. `bot_dm_status` takes the block
as a parameter rather than fetching it, so the status message stays a pure function of the store.

##### Testing

`test_usage.py` (22) is written against **captured real CLI output**, because the module is a
parser for someone else's prose — a hand-written sample that happens to match the regex would test
the regex against itself and nothing more. The load-bearing assertions are the negative ones: that
the contributing breakdown yields no limits, that unrecognised output reads as *could not tell*
rather than as 0%, and that an engine failure returns a line instead of raising. The composition is
asserted where it lands, on `bot_dm_status`, including that an open review is not buried under it.

Also driven for real against the live CLI, since every fixture in the suite is a recording of it.

Suite: 716 → **738**.

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

2. ~~Whether meeting notes should also trigger the glossary **git commit/push** the `/meeting` skill
   does~~ — **resolved (increment 5): no, append only.** A commit/push from the poller's background
   task is a silent failure waiting on credentials/network/conflicts, and decision 5 below makes it
   moot: the glossary now lives in the vault, which the owner already backs up in full to a private
   repo. See *The decisions, settled* → 3. *(resolved)*
3. ~~Base document converter~~ — **resolved: PDF only, read natively by Claude Code.** No library
   (owner's call — library output is mediocre on layout), no shell-armed agent, and no LibreOffice:
   the owner exports to PDF from MS Office. Probed: 3 turns / ~$0.041 / zero tool denials / no
   permission mode. See *Next up: increment 2*. *(resolved)*

5. ~~Whether to physically **share** the meeting-transcriber glossary file or copy/symlink it.~~ —
   **resolved (increment 5, owner's call): neither — it moves into the vault**, at
   `~/Documents/MarkNotes/.claude/contextbot/glossary.md` (`GLOSSARY_PATH` overrides). Sharing
   meeting-transcriber's copy would tie the bot to a project the owner expects to stop using;
   committing one here would publish real names to a public repo; a gitignored local copy would
   never be backed up. The vault is private, already backed up wholesale to `marknotes-data.git`,
   and invisible to MarkNotes under `.claude/` (verified both). See *The decisions, settled* → 4.
   *(resolved)*

6. ~~**Should the bot DM answer when nobody asked it anything?**~~ — **resolved: yes, always, and
   the rule has no exceptions.** *(Raised by the owner in increment 4, deliberately not built there;
   settled after Phase 2 merged.)*

   The owner asked the sharper question first: **is there a BotFather/server setting that
   auto-replies while the bot is off?** Checked against the Bot API rather than assumed — there is
   nothing: no `away`, no `greeting`, no autoresponder of any kind. The adjacent features do not
   substitute. `setMyDescription` only shows in an *empty* chat, so the owner (who pressed Start
   long ago) would never see it; `setMyCommands` is a client-side menu that replies to nothing; and
   `setWebhook` needs a server that is up 24/7, which is the exact opposite of this app's "no
   background operation" goal. **A Telegram bot is not a server-side entity — it is a token plus
   your code.** When the code is down, nothing answers, and nothing can be made to.

   So the achievable half is the whole of it, and it turns out to be enough: **answer every message,
   without fail, while the app is up.** Then silence stops being ambiguous and starts being
   information — *nothing is running*. That is the honest version of what the owner wanted, and it
   is strictly better than the old behaviour, where silence read the same whether the bot was
   stopped, broken, or simply uninterested. Nothing is lost in the meantime either: the Bot API
   retains updates for 24h, so a message sent to a stopped bot is queued and answered on the next
   Start.

   **What it took:**

   - `core/review_poller.py` → **`core/bot_dm_poller.py`** (`ReviewPoller` → `BotDmPoller`). The
     name was the old *activation rule*, not the module's identity, and keeping it would have made
     the name lie.
   - **`is_active` is the client's connection**, not `store.has_pending`. `stop()` disconnects, so
     the loop needs no flag of its own to go stale.
   - **The backlog filter reports instead of dropping.** This is the part worth reading twice. The
     poller hands on `(text, answerable)`; dropping a stale message *here* would have rebuilt the
     same silence one layer down — app off, owner types `확인`, app starts and opens a review during
     catch-up, and the message they sent hours earlier vanishes without a word. It is answered, and
     told **why** it was not applied, which matters because they very likely typed `확인` at a
     question they had not yet been shown. Driven for real: the stale `확인` did not accept the
     draft and wrote no note.
   - **`conversation.bot_dm_status()`** — plain text, deliberately. `Notifier.send` passes no
     `parse_mode`, so `**bold**` would arrive as asterisks; and *turning it on would be worse than
     untidy*, because note filenames are full of underscores
     (`260716-회의-하반기_모두의_ai_전략_덱_검토.md`), Markdown reads those as italics, and a parse
     error makes Telegram reject the message — which `Notifier` **swallows**. A confirmation would
     vanish rather than fail.

   **Replies are per message, not coalesced** (owner's call). A batch summary would be quieter, but
   it would hand the ambiguity straight back: if the bot sometimes answers and sometimes does not,
   silence means nothing again.

   **Still open — deliberately:** should a plain message to the bot DM *start* something? This is
   the mechanical half only: the bot reports and points at `Saved Messages`, and does not act. That
   remains the natural end of the "capture vs conversation" split, and the plumbing is now in place
   for it — a producer would hook where `bot_dm_status` currently answers. Also unbuilt:
   `setMyCommands` (`/status`, `/help`), which the owner declined for now — anything you type
   already gets the status, so there is no command to discover yet.

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

The owner can now **see** that pool from where they use the bot: the idle bot-DM status reports both
windows and their reset times, read from the CLI's own local `/usage`. See *The post-increment
feature: the idle status reports what is left of the plan*.

## Testing strategy

Mock `engine/claude_cli.py` and the STT step so the conversation state machine, session
resume/finalize, glossary update, original-file moves, and file output are all testable without
real model runs, model downloads, or network. Add fixtures for pending-session persistence and
timeout/`취소` handling. *(Held, all five increments.)*

Increment 4 adds `test_conversation.py` (the state machine: the draft turn, the resume turn, accept
/ cancel / revise, and **every failure edge** — a usage limit on turn 1 deferring *and leaving no
review behind*, a usage limit on turn 2 keeping the review alive, a lost session delivering the
draft, the give-up counter, expiry), `test_session_store.py` (the work-dir isolation, the file as
authority across two store instances, restart survival, `created_at` vs `source_date`), and
`test_bot_dm_poller.py` (the owner filter, **the backlog filter and the restart case it protects**,
offset advance, the tick, transport errors — renamed with its module, and the backlog cases rewritten
when decision 6 made that filter *report* rather than drop). `ClaudeSessionLost` is covered in `test_claude_cli.py`
against the fake-CLI subprocess, the two review templates in `test_prompts.py` (including the tilde
rule, which is parametrized per template so a new one cannot forget it), the prefix dispatch in
`test_router.py`, and the poller lifecycle + the HWM-advances-on-review rule in
`test_client_service.py`. Suite: 341 → **474**.

Increment 5 adds `test_stt.py` (the model-presence check incl. **a half-finished download**, the
resolved-directory-not-repo-id rule that keeps transcription offline, ffmpeg onto PATH, and every
`TranscriptionError`), `test_glossary.py` (parsing the table, appending only what is new, and never
corrupting it — pipes, dupes, and landing *inside* the table rather than at EOF), and
`test_audio_handler.py` (the happy path, the isolation asserted **as the model saw it**, the
transcript cache proving Whisper runs once across a deferral and its replay, every degradation
landing on a transcript note, and the queue). `test_conversation.py` gains the queue, `split_tags`,
`strip_preamble`/`strip_trailing_rule` (both from real runs), and what accepting a meeting now
means; `test_client_service.py` the promotion wiring; `test_health.py` the `whisper-stt` and
`glossary` probes, and `test_ui_smoke.py` the health panel's height (the one bug a screenshot
caught and the suite could not). **`mlx_whisper` is faked at the import site and `conftest.fake_stt` is required
by any test that routes audio** — without it a test that merely proves *dispatch* would load 1.5GB
of weights, and the `whisper-stt` probe is pinned so a green suite never means "the owner happens to
have the model on disk". `test_ui_smoke.py` grew from 3 tests to 27 and `test_bot_worker.py` is new
— the window is where this increment's last four bugs were, and every one was a layout constant or a
race that raised nothing. Suite: 474 → 601, then **636**: the `#검토` tests went with their subject
(573), and the rest is the audio pipeline's own edges plus the window's.

The post-increment UTC fix adds `test_localtime.py` and `test_audio_meta.py` (real files, real
`ffprobe` — see that section for why a mock would have been worthless), plus the dating cases in
`test_router.py`, `test_audio_handler.py`, and `test_config.py`. Suite: **689**. The assertions are
all about the *rendered wall clock* and the *filename*, because that is the only place the fault was
ever visible — the stored instant was right the whole time.

Captions add `test_captions.py` (716), and the usage readout `test_usage.py` (**738**) — the latter
parametrized over *captured* CLI output for the reason given in its own section: it is a parser for
prose this project does not own, so a fixture written to fit the pattern would prove nothing.
