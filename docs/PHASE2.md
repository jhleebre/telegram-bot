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

- [ ] **1. `claude -p` engine** (`engine/claude_cli.py`) — the shared subprocess wrapper + a trivial
      smoke use (e.g. LLM-enriched text title/tags). Nothing else depends on this until it works.
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
  claude -p "<prompt>" --output-format json --model <model> \
         --add-dir <workdir> --add-dir <glossary_dir> [--permission-mode <mode>]
  ```
- Parse the JSON result to capture **`session_id`** and the text output. Enforce a per-job
  timeout with cancellation.
- Resume for the next turn (keeps full prior context — transcript, draft, glossary):
  ```
  claude -p --resume <session_id> "<user reply>" --output-format json
  ```

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

## Open decisions to confirm before building Phase 2

1. **File-writing responsibility** — let `claude -p` write the note/glossary directly (needs a
   permissive `--permission-mode` + `--add-dir`) **or** have Claude return text and the bot write
   files (safer, more testable). *Leaning: bot writes.*
2. Whether meeting notes should also trigger the glossary **git commit/push** the `/meeting` skill
   does (both the vault and meeting-transcriber are git repos).
3. Base document converter: `markitdown` vs `pandoc`-based vs Claude-skill-driven.
4. Claude `--model` and per-job timeout budgets.
5. Whether to physically **share** the meeting-transcriber glossary file or copy/symlink it.

## Testing strategy

Mock `engine/claude_cli.py` and the STT step so the conversation state machine, session
resume/finalize, glossary update, original-file moves, and file output are all testable without
real model runs, model downloads, or network. Add fixtures for pending-session persistence and
timeout/`취소` handling.
