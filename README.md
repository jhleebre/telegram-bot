# Personal Telegram Context Bot

A single-user tool that turns messages and files you send into Markdown notes in your personal
knowledge base (`~/Documents/MarkNotes/0_inbox/`).

It uses **two Telegram channels for two jobs**:

- **Input — your "Saved Messages":** read via your own account (Telethon). Saved Messages is
  retained indefinitely and its history is readable, so **messages sent while the app was closed —
  even over a weekend — are processed the next time you open the app.** Only you can write to your
  own Saved Messages, so single-user security is inherent.
- **Reply — your bot's DM:** a bot sends confirmations (and, in Phase 2, review questions) to your
  bot chat. Replying *into* Saved Messages would loop, so replies use the separate bot channel.

The bot **does not run in the background** — it only reads while the desktop app window is open.
The window is a single compact row: a round start/stop button, what the bot is doing, and a health
light. Expand it (`⌄`) when the light is amber or red, or when you want the activity log.

## Why this design

The Telegram **Bot API** only keeps undelivered updates for ~24h and **can't read chat history**,
so a bot alone would lose messages sent while the laptop sleeps over a weekend. A **user-account**
client can read Saved Messages history, which removes the 24h limit entirely.

## Status

- **Phase 1 (implemented):** desktop app, Saved Messages ingestion with catch-up + live, bot-DM
  confirmations, health checks, and **text → Markdown note**.
- **Phase 2 (complete — built one increment at a time):**
  - ✅ **1. `claude -p` engine** — shared headless-CLI wrapper, plus its first use: text notes now
    get an **LLM-derived title, tags, and summary**. Falls back to the Phase 1 path (first line as
    title) when the CLI is missing, slow, or disabled, so the bot still works offline.
  - ✅ **2. Documents → Markdown** — send a **PDF** and Claude Code reads it natively into a
    Markdown note; **`.txt`/`.csv`** become notes too (CSV tables are rendered exactly, never
    retyped by a model), and a **`.md`** file is saved as-is. Originals are filed to
    `~/Downloads/`. Anything else (docx/pptx/xlsx) gets a reply asking for a PDF export.
  - ✅ **3. Image → described note** — send a screenshot or photo and it becomes a note with the
    image **embedded in it** (base64) above a Claude-generated description and a verbatim
    transcription of any text in it. `heic`/`bmp` are converted first, because neither the model
    nor the vault can render them. A failure still saves the image, with the reason in place of the
    description — the capture is never lost.
  - ✅ **4. Human-in-the-loop review** — the bot drafts a note, then asks you about anything it had
    to guess. Reply in the bot DM: `확인` saves it, `취소` drops it, and anything else is applied and
    shown to you again. Built on resumable Claude Code sessions, so each turn keeps full context,
    and a review **survives closing the app**. If it can't be finished, the draft is saved anyway
    and marked unreviewed — a review never ends empty-handed. *(Built and verified on a `#검토 <메모>`
    prefix, which was scaffolding; increment 5 replaced it with audio and deleted it.)*
  - ✅ **5. Audio → meeting note** — send a recording and it is transcribed **on your machine**
    (`mlx-whisper`; no audio ever leaves it), corrected against your glossary, and drafted into a
    structured meeting note — then reviewed with you over the bot DM (increment 4's loop). `확인`
    saves it, files any terms the review confirmed into the glossary, and deletes the recording.
    Send two recordings and the second is **queued**, not refused: it is transcribed and drafted
    right away, and you are asked about it when the first review ends. See below — **the Whisper
    model is a one-time download you have to run**.

  **Type a caption with any attachment and it steers the note.** Telegram lets you add one to
  every kind of media — photo, recording, voice note, PDF, `.txt`, `.csv` — and the bot reads it as
  *you talking to it*, not as text to file. So it acts on what you wrote rather than pasting it in:
  "핵심만 짧게" shortens the note, "참석자는 김철수, 이영희" fills the meeting note's Overview and
  stops it asking you who was there, "작년 버전이라 숫자는 옛날 것" ends up as context in the note
  itself. Facts you supply beat the bot's guesses from the file. Your exact words are kept in the
  note's `caption:` frontmatter, since acting on them means they do not survive in the body.

  Two limits worth knowing. A caption **cannot** make the bot claim it read something it could not,
  skip a required section, or — on a PDF — replace the conversion with a summary: ask for one and
  you get a `## 요약` section *added* above the full document, because the original goes to
  `~/Downloads` and the note is the only searchable copy. And a sent **`.md` ignores its caption**:
  that route saves your file byte-for-byte and runs no model, so there is nothing to act with and
  nothing that may edit your bytes.

  The bot DM **answers everything you send it** while the app is running — a status, or your
  review reply. Telegram has no way to reply for a bot that is switched off, so this is the next
  best thing: **silence means it is not running.** (Nothing is lost meanwhile — Telegram queues
  your message for 24h and the bot answers it on the next Start.)

  Notes are filed the way the rest of your vault is: **`YYMMDD-<분류>-<제목>.md`**. A recording is
  `회의`, a message or screenshot is `노트`, and a document is classified from its own content into
  one of `전략` / `기획` / `조사` / `안건` / `보고` / `초안`.

  See [docs/PHASE2.md](docs/PHASE2.md).

  **Documents are PDF-only by design.** Export from Word/PowerPoint to PDF and send that — Claude
  Code reads PDFs natively, so the bot needs no converter, no extra dependency, and never has to
  grant the model shell access to open a file.

### What happens when the Claude usage limit runs out

The bot **stops itself and leaves the message unprocessed** rather than saving a weaker note:

```
⏸ 사용량 한도 — 리셋 후 Start를 눌러주세요
```

Nothing is lost — the message stays in your Saved Messages. Once the limit resets, press **Start**
and the bot resumes from exactly that message, in order. Claude Code usage on a Pro/Max plan draws
from your subscription's usage limits, not from API billing, so this costs nothing beyond the
subscription (usage credits are opt-in and off by default).

## Setup

```bash
cd ~/Projects/telegram-bot

python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # runtime + test deps

cp .env.example .env      # then fill in the values below
```

### What you must provide (in `.env`)

1. **`TELEGRAM_API_ID` / `TELEGRAM_API_HASH`** — create an app at
   <https://my.telegram.org> → *API development tools*. This is for reading your Saved Messages.
2. **`TELEGRAM_BOT_TOKEN`** — create a bot with **@BotFather**, then **press Start on the bot once**
   (so it can DM you). This is for replies.

Phase 2 also uses the **`claude` CLI** (already installed) to enrich notes. It needs no
configuration — but `CLAUDE_MODEL`, `CLAUDE_TIMEOUT_SEC`, and `CLAUDE_ENABLED=false` (fully
offline, no LLM) are available in `.env`.

**Notes are dated on your clock, not Telegram's.** Telegram reports every message's time in UTC, so
`NOTE_TIMEZONE` (default `Asia/Seoul`) is what turns that into the time you actually read — it sets
the frontmatter `date:`, the `YYMMDD-` in the filename, and a meeting note's Overview table. Any
IANA zone name works; an unknown one stops the bot at startup rather than guessing, because the
symptom of a wrong zone is a note that is confidently off by hours.

### One-time: the Whisper model (only if you send audio)

`pip install` does **not** bring the speech-recognition model — `mlx-whisper` takes a HuggingFace
repo id and would download ~1.5GB the first time it transcribes something. The bot refuses to let
that happen inside a job you are waiting on, so fetch it once:

```bash
.venv/bin/python scripts/download_model.py          # --check first, to see if you need it
brew install ffmpeg                                  # needed to decode audio
```

Until you do, the health panel reports **`whisper-stt` — model not downloaded**, and audio messages
are answered with the reason. Everything else works without it.

The **glossary** (`GLOSSARY_PATH`) is what stops speech recognition from quietly renaming things —
it hears "티맵" as "팀웹". It defaults to `~/Documents/MarkNotes/.claude/contextbot/glossary.md`,
inside the vault: private, backed up with the rest of your notes, and hidden from MarkNotes (which
skips dotfolders) so it is not a note. It grows on its own — when you correct a name during a
review, that correction is filed. No glossary is fine; you just get no term corrections.

**A recording is dated by the recording, when it can be.** Meeting notes are the one place the
capture time is the wrong answer: you record at 14:30 and upload in the evening, and `NOTE_TIMEZONE`
would only ever give you the upload. If the file's own metadata says when *and where* it was made —
iOS writes `com.apple.quicktime.creationdate` with a real UTC offset — the meeting is dated by that
instead, so one recorded abroad keeps the clock of the room it happened in. Most recordings say
nothing (a Telegram voice message is re-encoded and arrives with no metadata at all), and those fall
back to the upload time on your `NOTE_TIMEZONE` clock. Send a real meeting **as a file** to get the
better answer.

**Long meetings are fine, and slow is normal.** A real ~1-hour meeting measures about 13 minutes end
to end — 5 of Whisper, 7 of drafting — and the bot tells you it got the recording, then goes quiet
until the draft is ready. The drafting budget (`CLAUDE_MEETING_TIMEOUT_SEC`, default 1 hour) is a
*hang detector*, not a schedule: being generous costs nothing, and being tight would throw the whole
transcription away at the last step. One reviewed meeting costs roughly $1 of *plan usage* (not
money — see above), and each further correction ~$0.40, because the revise turn re-reads the
transcript.

### One-time login (you run this yourself)

```bash
.venv/bin/python login.py
```

This logs into **your** Telegram account interactively — it prompts for your phone number, the
login code Telegram sends you, and your 2FA password if you have one. It creates
`state/contextbot.session` and records a baseline so your *existing* Saved Messages history is not
imported. Only messages you send **after** this point become notes. (Nothing here is automated —
you enter your own credentials.)

## Run

```bash
.venv/bin/python run.py
```

The window opens as one row: a green **▶** button, and the line `잠자는 중 — Start를 눌러
깨워주세요`. Click it; the button turns into a red **■** and the row says what the bot is doing.

The dot on the right is the **health light**: green is fine, amber and red mean expand, and grey
means nobody is checking (stopped, or not checked yet — it is never green on a bot that is not
running). Hovering it names the probe (`🟡 whisper-stt`); `⌄` shows all seven and the activity log,
`⌃` shrinks back.

Most failures are **degraded, not error**: a missing `claude` CLI still captures notes (just without
LLM titles/tags), and a missing Whisper model only stops audio. The **claude-engine** probe shows the
resolved CLI path and model; **whisper-stt** shows the model and the resolved `ffmpeg`.

Now open Telegram and send a text to **Saved Messages** ("note to self"). A `.md` note appears in
your inbox and your **bot DM** replies `📝 저장됨: <filename>`. Close the window to stop reading.

**Offline messages survive.** Anything you send to Saved Messages while the app is closed — even
after several days — is processed in order the next time you Start the app.

### Add to the Dock (launch without Terminal)

Build a `Context Bot.app` bundle once:

```bash
./scripts/build_app.sh
```

This creates `Context Bot.app` (robot icon) in the project folder. Double-click it in Finder to
launch, then right-click its Dock icon → **Options → Keep in Dock** (or drag the `.app` onto the
Dock). From then on, one click on the Dock icon opens the app — no `run.py`, no Terminal.

The bundle just launches the project's `.venv` Python + `run.py`, so keep the project folder in
place. Re-run `./scripts/build_app.sh` if you move the project. To build into a different location
(e.g. your Applications folder): `./scripts/build_app.sh ~/Applications`.

## Test

```bash
.venv/bin/pytest
```

No real Telegram connection, session, or token is needed; the suite uses fake Telethon/bot objects
and temporary inbox directories. In a headless environment, set `QT_QPA_PLATFORM=offscreen`.

## Project layout

```
src/contextbot/
├── config.py             # settings from env/.env (api id/hash, bot token, inbox, note timezone)
├── localtime.py          # the clock a note is written on (Telegram reports UTC; you don't read it)
├── logging_setup.py      # file + in-memory (UI) logging
├── core/
│   ├── client_service.py # Telethon input (catch-up + live) + dispatch + bot reply
│   ├── session_store.py  # the pending review: draft, resume handle, work dir, queue
│   ├── bot_dm_poller.py  # bot-DM polling, whenever the app is up
│   ├── notifier.py       # send-only bot → owner DM
│   ├── hwm.py            # high-water-mark (last processed Saved Messages id)
│   ├── router.py         # classify + route → handlers
│   ├── health.py         # 7 probes: auth / bot-token / inbox / connection /
│   │                     #   claude-engine / whisper-stt / glossary
│   ├── security.py       # Saved-Messages self-peer guard
│   └── status.py         # observable status model
├── engine/
│   ├── claude_cli.py     # async wrapper over headless `claude -p` (JSON result, sessions)
│   ├── parsing.py        # recover a JSON object from a model's free-text reply
│   └── prompts/          # prompt templates (*.md)
├── handlers/             # text / document / image / audio handlers + the review conversation
├── stt/                  # local speech-to-text (mlx-whisper), ported in — no external project
├── files/                # original-file policy (→ Downloads), encoding / CSV, images, glossary,
│                         #   audio metadata (when a recording says where it was made)
├── notes/                # frontmatter, filename (YYMMDD-<분류>-slug), atomic writer
└── ui/                   # compact status bar + asyncio worker thread
login.py                  # one-time interactive login (user-run)
run.py                    # app entrypoint
scripts/                  # build_app.sh (Dock .app bundle), make_icon.py, download_model.py
tests/                    # pytest suite
docs/                     # PHASE1.md, PHASE2.md
```

See [docs/PHASE1.md](docs/PHASE1.md) for the detailed design.
