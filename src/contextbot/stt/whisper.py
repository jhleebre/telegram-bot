"""Local speech-to-text via `mlx-whisper`, ported from meeting-transcriber's `transcribe.py`.

The port is deliberate, not incidental: the bot must not depend on `~/Projects/meeting-transcriber/`
at runtime (docs/PHASE2.md, increment 5). What changed on the way in, and why:

- **The function raises instead of calling `sys.exit`.** The reference is a CLI script; a handler
  needs an exception it can degrade on.
- **It refuses to download the model.** See :func:`model_is_cached` — this is the whole point of the
  module, and the reason it is written down here rather than left to `mlx_whisper`.
- **`ffmpeg` is put on `PATH` rather than passed in.** See :func:`_ffmpeg_on_path`.
- **The transcript rendering drops the reference's "Full text" trailer.** That trailer repeats the
  entire transcript a second time under the timestamped lines; the model reads this file, so
  duplicating it only doubles the tokens for content it already has.

Everything here is **synchronous and slow** (minutes for a long recording). Callers must run it off
the event loop — ``asyncio.to_thread`` — or the UI stops repainting and Telethon's connection stalls
while a meeting transcribes.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..engine.claude_cli import resolve_executable

logger = logging.getLogger("contextbot.stt.whisper")

# The HF repo id meeting-transcriber uses. **This is not a package dependency** — `mlx_whisper`
# fetches it from HuggingFace on first use, so nothing in requirements.txt makes it appear.
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_LANGUAGE = "ko"

# Roughly what the weights cost, for messages that ask the owner to fetch them.
MODEL_SIZE_HINT = "약 1.5GB"


class TranscriptionError(Exception):
    """STT could not run, or could not finish. Always carries a reason the owner can act on."""


@dataclass(frozen=True)
class Segment:
    """One timestamped chunk of speech."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    """A finished transcription."""

    text: str
    segments: list[Segment] = field(default_factory=list)
    language: str = DEFAULT_LANGUAGE
    processing_time: float = 0.0

    @property
    def is_empty(self) -> bool:
        """True when the audio yielded no words — silence, or a file that is not really speech."""
        return not self.text.strip()


def format_timestamp(seconds: float) -> str:
    """Seconds → ``MM:SS``, or ``H:MM:SS`` past an hour. Ported verbatim in behaviour."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def render_transcript(transcript: Transcript, *, source_name: str, model: str = DEFAULT_MODEL) -> str:
    """The transcript as the text file the note-drafting prompt reads.

    The timestamps are load-bearing rather than decoration: the review block cites them so the owner
    can scrub to a flagged word in the recording and hear what was actually said. meeting-transcriber
    got them by grepping this file; here the model simply reads it, which is one fewer reason to
    hand a shell to a job whose input is a recording of other people talking.
    """
    lines = [
        f"# Transcript: {source_name}",
        f"# Model: {model}",
        f"# Language: {transcript.language}",
        f"# {'=' * 60}",
        "",
    ]
    for seg in transcript.segments:
        lines.append(f"[{format_timestamp(seg.start)} -> {format_timestamp(seg.end)}] {seg.text}")
    if not transcript.segments:
        # No segments but text anyway: keep the words rather than emitting a header-only file.
        lines.append(transcript.text.strip())
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- ffmpeg
def resolve_ffmpeg(executable: str = "ffmpeg") -> str | None:
    """Absolute path to `ffmpeg`, or None. Same resolution the `claude` CLI needs, for one reason.

    A Dock-launched app inherits launchd's minimal PATH (`/usr/local/bin:/bin:/usr/bin`), and
    Homebrew's ffmpeg lives in `/opt/homebrew/bin` — invisible to it. This is the increment-1 PATH
    bug's third appearance, and the first two were both found only in the real app.
    """
    return resolve_executable(executable)


def _ffmpeg_on_path(executable: str = "ffmpeg") -> str:
    """Make `ffmpeg` findable by `mlx_whisper`, and return where it was found.

    **Resolving it is not enough, and this is why the module owns the problem.** `mlx_whisper.audio.
    load_audio` builds its command as the bare string `["ffmpeg", "-nostdin", "-i", file, …]` and
    runs it with the inherited environment — there is no parameter to hand a path to. So the only
    way to make a Dock-launched app find Homebrew's ffmpeg is to put its directory on `PATH` before
    `mlx_whisper` shells out. Prepending is idempotent, so repeated calls cost nothing.
    """
    found = resolve_ffmpeg(executable)
    if found is None:
        raise TranscriptionError(
            f"{executable!r}를 찾을 수 없습니다 — 오디오를 디코딩할 수 없습니다 "
            "(설치: brew install ffmpeg)"
        )
    directory = str(Path(found).parent)
    current = os.environ.get("PATH", "")
    if directory not in current.split(os.pathsep):
        os.environ["PATH"] = f"{directory}{os.pathsep}{current}" if current else directory
        logger.debug("added %s to PATH so mlx_whisper can find ffmpeg", directory)
    return found


# ---------------------------------------------------------------------------- model
def _snapshot_is_complete(path: Path) -> bool:
    """True when a model directory holds what `mlx_whisper.load_models.load_model` will open.

    Read off that function rather than guessed: it opens `config.json`, then `weights.safetensors`
    falling back to `weights.npz`. Checking those exact files is what makes a **half-finished
    download** count as missing — an interrupted fetch leaves the directory sitting there, and a
    check for the directory alone would call it present and then fail inside the job.
    """
    if not (path / "config.json").is_file():
        return False
    return (path / "weights.safetensors").is_file() or (path / "weights.npz").is_file()


def resolve_model_dir(model: str = DEFAULT_MODEL) -> Path | None:
    """The local directory holding the weights, or None if they are not (fully) downloaded.

    **This is the trap the whole module exists for.** `mlx_whisper` takes an HF *repo id*, not a
    file, and `load_model` calls `snapshot_download(repo_id=…)` when it is not cached — silently, on
    first use. So nobody ever "installs" the model; it arrives as a side effect of somebody's first
    meeting note, which stalls for minutes mid-job and fails outright offline. On the owner's machine
    it is already cached (meeting-transcriber put it there), so this can never fail in development —
    the same shape as increment 1's Dock PATH bug, which development also could not catch.

    Local, offline, and free: `local_files_only=True` never touches the network.

    Returning the **path** rather than a bool is what lets :func:`transcribe` stay offline — see
    there.
    """
    local = Path(model).expanduser()
    if local.is_dir():  # an explicit path to weights, rather than a repo id
        return local if _snapshot_is_complete(local) else None

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        return None  # no huggingface_hub means no mlx-whisper either — the package probe says so

    try:
        path = Path(snapshot_download(repo_id=model, local_files_only=True))
    except Exception:  # noqa: BLE001 - not cached, or the cache is unreadable; both mean "no"
        return None
    return path if _snapshot_is_complete(path) else None


def model_is_cached(model: str = DEFAULT_MODEL) -> bool:
    """True when the weights are already on disk, so transcribing will not hit the network."""
    return resolve_model_dir(model) is not None


def package_is_installed() -> bool:
    """True when `mlx_whisper` can be imported. Kept separate from the model: different fixes."""
    try:
        import mlx_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def download_model(model: str = DEFAULT_MODEL) -> Path:
    """Fetch the weights **deliberately**, and return where they landed. Needs the network.

    The explicit acquisition step the reference implementation never had. Everything else in this
    module exists to make sure this is the only place a download can happen.
    """
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise TranscriptionError(
            "huggingface_hub이 없습니다 — pip install -r requirements.txt 를 먼저 실행하세요"
        ) from exc
    logger.info("downloading Whisper model %s (%s)…", model, MODEL_SIZE_HINT)
    try:
        path = Path(snapshot_download(repo_id=model))
    except Exception as exc:  # noqa: BLE001
        raise TranscriptionError(f"모델을 내려받지 못했습니다: {exc}") from exc
    if not _snapshot_is_complete(path):
        raise TranscriptionError(f"모델을 내려받았지만 파일이 불완전합니다: {path}")
    return path


# ----------------------------------------------------------------------- transcribe
def transcribe(
    audio_path: Path | str,
    *,
    model: str = DEFAULT_MODEL,
    language: str = DEFAULT_LANGUAGE,
    word_timestamps: bool = True,
) -> Transcript:
    """Transcribe one audio file locally. **Blocking** — call it via `asyncio.to_thread`.

    Raises :class:`TranscriptionError` for every failure, including the two that would otherwise be
    invisible: a missing model (which `mlx_whisper` would silently download) and a missing `ffmpeg`
    (which it would report as a bare `FileNotFoundError` from a subprocess three frames down).
    """
    audio = Path(audio_path).expanduser()
    if not audio.is_file():
        raise TranscriptionError(f"오디오 파일이 없습니다: {audio}")

    try:
        import mlx_whisper
    except ImportError as exc:
        raise TranscriptionError(
            "mlx-whisper가 설치되어 있지 않습니다 — pip install -r requirements.txt"
        ) from exc

    # Refuse rather than download. A 1.5GB fetch must never happen inside a job the owner is waiting
    # on: it stalls for minutes with no explanation and fails outright with no network. The health
    # probe surfaces this *before* they send a recording; this is the backstop for when they did.
    model_dir = resolve_model_dir(model)
    if model_dir is None:
        raise TranscriptionError(
            f"Whisper 모델({model})이 아직 없습니다 ({MODEL_SIZE_HINT}). "
            "회의 도중에 내려받지 않도록 전사를 중단했습니다 — "
            "`.venv/bin/python scripts/download_model.py` 로 먼저 받아주세요."
        )

    _ffmpeg_on_path()

    started = time.time()
    try:
        result = mlx_whisper.transcribe(
            str(audio),
            # The resolved **directory**, not the repo id — and the difference is a network call.
            # `load_model` does `if not Path(path_or_hf_repo).exists(): snapshot_download(...)`, and
            # snapshot_download phones HuggingFace to resolve `main` **even when the model is fully
            # cached**. Measured: a transcription with the weights already on disk still issued
            # three requests to huggingface.co. Handing it a real path takes that branch out
            # entirely, so a cached model transcribes offline and costs no round-trip. Checking the
            # cache and then passing the repo id anyway would have left the offline failure we just
            # spent this module preventing.
            path_or_hf_repo=str(model_dir),
            language=language,
            word_timestamps=word_timestamps,
            verbose=False,
        )
    except TranscriptionError:
        raise
    except Exception as exc:  # noqa: BLE001 - mlx/ffmpeg failures are RuntimeError-shaped
        raise TranscriptionError(f"전사에 실패했습니다: {exc}") from exc
    elapsed = time.time() - started

    segments = [
        Segment(start=float(s["start"]), end=float(s["end"]), text=str(s["text"]).strip())
        for s in result.get("segments", [])
    ]
    transcript = Transcript(
        text=str(result.get("text") or "").strip(),
        segments=segments,
        language=str(result.get("language") or language),
        processing_time=elapsed,
    )
    logger.info(
        "transcribed %s in %.1fs: %d chars, %d segments",
        audio.name,
        elapsed,
        len(transcript.text),
        len(segments),
    )
    return transcript
