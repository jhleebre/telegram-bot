# Phase 1 — Design (as built)

Phase 1 delivers a personal, single-user capture tool wrapped in a small PySide6 desktop app, with
health checks and **text → Markdown note** saving. Audio/image/document handling is stubbed with a
"Phase 2 예정" reply. Everything except pixel-drawing is unit-tested.

## Goals

- **Single user only** — input is read from the owner's own **Saved Messages**; only the owner can
  write there, so security is inherent (no allowlist needed).
- **No background operation** — the app reads only while its window is open; closing it disconnects
  the client.
- **Survive weekends** — Saved Messages is durable and history-readable, so messages sent while the
  app was closed are processed on the next Start (no 24h Bot-API limit).
- **Graphical status** — a colored light + label (STOPPED / STARTING / RUNNING / PROCESSING /
  ERROR), a health panel, and a live log view.
- **Text memo → Markdown** — saved to `~/Documents/MarkNotes/0_inbox/` with YAML frontmatter; a
  confirmation is sent to the owner's **bot DM**.

## Architecture — two channels, one asyncio loop

```
Saved Messages ──Telethon(user)──▶ catch-up + live ──▶ route()/handlers ──▶ Markdown note → inbox
   (durable input)                        │                                        │
                                          └──────────── result.reply ──────────────┘
                                                          │
                                    Bot API (send-only) ──▶ owner's bot DM  +  UI log
```

- **`BotWorker(QThread)`** owns an asyncio loop and the `ClientService`; Qt control methods use
  `run_coroutine_threadsafe`, and the `StatusModel` observer re-emits as Qt signals (queued to the
  main thread). Closing the window calls `shutdown()` → disconnect + stop loop + join thread.
- **Input** (Telethon user client): on Start, `catch_up()` reads Saved Messages with
  `id > high-water-mark` oldest-first; while open, live `events.NewMessage` (filtered to the self
  peer) feeds the same pipeline.
- **Reply** (send-only `telegram.Bot`): posts confirmations to the owner's bot DM. It never polls,
  so the Bot API's 24h limit never touches capture. Owner chat id is derived from the user account's
  own id (`get_me()`), overridable via `OWNER_CHAT_ID`.

## Modules (`src/contextbot/`)

| Module | Responsibility |
|--------|----------------|
| `config.py` | `Settings.load()` — `api_id`/`api_hash`/`session_path` (Telethon), `telegram_bot_token` (reply bot), `owner_chat_id?`, `inbox_dir`, `log_level`. Secrets hidden from `repr`. |
| `core/client_service.py` | Telethon lifecycle (`start`/`stop`), catch-up, live events, dispatch via `route()`, reply via notifier, HWM advance, status transitions. Client/bot injectable for tests. |
| `core/notifier.py` | Send-only bot wrapper → owner DM. |
| `core/hwm.py` | `HighWaterMark` — persist last processed Saved Messages id (`state/hwm.json`); `baseline()` for first run. |
| `core/router.py` | `classify_document`, `build_incoming_message` (Telethon adapter), `route` dispatch via `ROUTING_TABLE`. |
| `core/health.py` | `HealthChecker.check()` → telethon-auth / bot-token / inbox / connection probes → ERROR/DEGRADED/HEALTHY. |
| `core/security.py` | `is_saved_messages(peer_id, my_id)` self-peer guard. |
| `core/status.py` | `BotStatus` enum, color/label/emoji/tagline maps, observable `StatusModel`. |
| `handlers/*` | `text_handler` (real), audio/image/document stubs; `base.py` (`IncomingMessage` with `raw` Telethon ref for Phase 2). |
| `notes/*` | `frontmatter` (PyYAML), `naming` (`YYMMDD-HHMM-slug.md` + collision suffix), `markdown_writer` (atomic write). |
| `ui/*` | `bot_worker`, `status_widget`, `main_window`, `app`; `run.py`; `login.py`. |

## Desktop app & Dock launcher

- **Friendly graphical UI** (`ui/status_widget.py`, `ui/main_window.py`): a card-based window with a
  big status "face" (per-status emoji on a colored disc that gently pulses while active), a friendly
  Korean tagline, a prominent green **Start** / red **Stop** button, and HEALTH + ACTIVITY cards.
  Styling is self-contained QSS with the `Fusion` style for consistent rendering across system
  themes. There is no in-window title header (the OS title bar carries the name).
- **Dock launcher** (`scripts/make_icon.py`, `scripts/build_app.sh`): `build_app.sh` renders a
  robot-emoji icon (PySide6 → PNG → `.icns` via `sips`/`iconutil`) and assembles a
  `Context Bot.app` bundle whose launcher runs the project's `.venv` Python + `run.py`. The owner
  double-clicks it once, then keeps it in the Dock — no Terminal needed. The bundle references the
  project by absolute path, so re-run the script if the project moves. Build artifacts (`build/`,
  `*.app/`) are git-ignored.

## Offline backlog & the high-water-mark

- On **first run** (no `state/hwm.json`), the HWM is **baselined to the latest Saved Messages id**,
  so the owner's pre-existing history is not converted into notes. `login.py` also sets this.
- Thereafter, the HWM is advanced to each message id **after** it is successfully processed, and
  persisted. On Start, only messages with `id > HWM` are processed (oldest-first), then live events
  take over. Catch-up/live overlap is de-duplicated by the HWM guard.
- **Correction (made during Phase 2 increment 1):** as originally built, `_process` swallowed every
  exception and catch-up kept going — so a later success advanced the *single* watermark past the
  failed message, and the `id > HWM` guard then skipped it forever. **That silently lost messages.**
  The HWM contract is now explicit: it advances on success, and on a *permanent* failure it advances
  deliberately with a bot-DM notice (`id=N … 건너뜁니다`) so nothing disappears quietly; a
  *transient* failure (`DeferMessage`, i.e. a Claude usage limit) leaves it untouched and halts the
  bot, so the message replays on the next Start. See the usage-limit policy in
  [PHASE2.md](PHASE2.md).
- Because Saved Messages is retained indefinitely, this works across arbitrary offline gaps — the
  ~24h Bot-API limit does not apply.

## Security model

- Input is the owner's own Saved Messages (self peer); the live handler ignores any other chat and
  `is_saved_messages` re-checks defensively. No allowlist is required.
- The login session and bot token live in `.env` / `state/` (git-ignored). Secrets never logged.

## Note format

```yaml
---
title: "<first non-empty line, trimmed to 80 chars>"
date: 2026-07-15T14:30:00+00:00
source: telegram
type: note
tags: []
telegram_message_id: <id>
---

<original message text>
```

## Tests (`tests/`, 88 cases)

- Pure logic: `test_config`, `test_security`, `test_status`, `test_frontmatter`, `test_naming`,
  `test_markdown_writer`, `test_hwm`.
- Async: `test_health`, `test_router`, `test_text_handler`, `test_notifier`, `test_client_service`
  — driven with fake Telethon messages/client and a fake bot (`conftest.py`); no network, no
  session, no token.
- UI: `test_ui_smoke` — `pytest-qt` on the offscreen platform; auto-skips if unavailable.

Run: `.venv/bin/pytest` (set `QT_QPA_PLATFORM=offscreen` in a headless environment).

## Manual verification

See the project README: set `.env`, run `login.py` (one-time), `run.py` → Start → green, send a
Saved Messages text → note appears in inbox + bot DM confirmation; Stop, send several, wait, Start →
backlog processed in order; audio/image/doc → "Phase 2 예정".
