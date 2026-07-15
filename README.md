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
A small PySide6 window shows live status (stopped / starting / running / processing / error) and a
health panel.

## Why this design

The Telegram **Bot API** only keeps undelivered updates for ~24h and **can't read chat history**,
so a bot alone would lose messages sent while the laptop sleeps over a weekend. A **user-account**
client can read Saved Messages history, which removes the 24h limit entirely.

## Status

- **Phase 1 (implemented):** desktop app, Saved Messages ingestion with catch-up + live, bot-DM
  confirmations, health checks, and **text → Markdown note**.
- **Phase 2 (in progress, one increment at a time):**
  - ✅ **1. `claude -p` engine** — shared headless-CLI wrapper, plus its first use: text notes now
    get an **LLM-derived title, tags, and summary**. Falls back to the Phase 1 path (first line as
    title) when the CLI is missing, slow, or disabled, so the bot still works offline.
  - ⬜ 2. documents (pdf/pptx/docx/…) → Markdown · ⬜ 3. images → described notes ·
    ⬜ 4. human-in-the-loop review plumbing · ⬜ 5. audio → meeting notes

  Audio/image/document still reply "Phase 2 예정". See [docs/PHASE2.md](docs/PHASE2.md).

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

The window opens with a **gray 😴 (stopped)** status face. Click **Start**; the face turns
**green 🤖 (running)** once health checks pass (telethon auth ✓, bot token ✓, inbox ✓, connected ✓,
claude-engine ✓).

The **claude-engine** probe shows the resolved CLI path and model. If it reports *not found*,
health is **degraded, not error**: notes are still captured, just without LLM title/tags/summary.

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
├── config.py             # settings from env/.env (api id/hash, bot token, inbox)
├── logging_setup.py      # file + in-memory (UI) logging
├── core/
│   ├── client_service.py # Telethon input (catch-up + live) + dispatch + bot reply
│   ├── notifier.py       # send-only bot → owner DM
│   ├── hwm.py            # high-water-mark (last processed Saved Messages id)
│   ├── router.py         # classify + route → handlers
│   ├── health.py         # auth / bot-token / inbox / connection probes
│   ├── security.py       # Saved-Messages self-peer guard
│   └── status.py         # observable status model
├── engine/
│   ├── claude_cli.py     # async wrapper over headless `claude -p` (JSON result, sessions)
│   ├── parsing.py        # recover a JSON object from a model's free-text reply
│   └── prompts/          # prompt templates (*.md)
├── handlers/             # text handler (+ LLM enrichment) + Phase 2 stubs
├── notes/                # frontmatter, filename, atomic markdown writer
└── ui/                   # PySide6 window + asyncio worker thread
login.py                  # one-time interactive login (user-run)
run.py                    # app entrypoint
scripts/                  # build_app.sh (Dock .app bundle) + make_icon.py
tests/                    # pytest suite
docs/                     # PHASE1.md, PHASE2.md
```

See [docs/PHASE1.md](docs/PHASE1.md) for the detailed design.
