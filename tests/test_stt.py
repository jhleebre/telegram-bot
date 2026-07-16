"""STT tests: no model is ever loaded, no audio is ever decoded, nothing is ever downloaded.

`mlx_whisper` is faked at the import site. What these assert is everything *around* the model call
— which is the whole reason the module exists, because the model call itself is the one part that
works fine on this machine and nowhere else.
"""

import os
import sys
import types
from pathlib import Path

import pytest

from contextbot.stt import whisper


# --------------------------------------------------------------------------- fakes
@pytest.fixture
def fake_mlx(monkeypatch):
    """Install a fake `mlx_whisper` module and hand back the calls it recorded."""
    calls: list[dict] = []

    def fake_transcribe(audio, **kwargs):
        calls.append({"audio": audio, **kwargs})
        return {
            "text": "안녕하세요. 회의를 시작합니다.",
            "language": "ko",
            "segments": [
                {"start": 0.0, "end": 3.5, "text": " 안녕하세요."},
                {"start": 3.5, "end": 7.2, "text": " 회의를 시작합니다."},
            ],
        }

    module = types.ModuleType("mlx_whisper")
    module.transcribe = fake_transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", module)
    return calls


@pytest.fixture
def cached_model(tmp_path, monkeypatch):
    """A complete model directory, and resolution pointed at it."""
    model = tmp_path / "weights"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "weights.safetensors").write_bytes(b"\x00")
    monkeypatch.setattr(whisper, "resolve_model_dir", lambda m=whisper.DEFAULT_MODEL: model)
    return model


@pytest.fixture
def audio(tmp_path) -> Path:
    path = tmp_path / "meeting.m4a"
    path.write_bytes(b"not really audio; nothing decodes it here")
    return path


@pytest.fixture(autouse=True)
def ffmpeg_present(monkeypatch):
    monkeypatch.setattr(whisper, "resolve_ffmpeg", lambda executable="ffmpeg": "/opt/homebrew/bin/ffmpeg")


# ------------------------------------------------------------------ model presence
def test_a_complete_snapshot_is_cached(tmp_path):
    model = tmp_path / "m"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "weights.safetensors").write_bytes(b"\x00")
    assert whisper.model_is_cached(str(model))
    assert whisper.resolve_model_dir(str(model)) == model


def test_weights_npz_counts_too(tmp_path):
    """`load_model` falls back to weights.npz, so a .npz-only snapshot is complete."""
    model = tmp_path / "m"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "weights.npz").write_bytes(b"\x00")
    assert whisper.model_is_cached(str(model))


@pytest.mark.parametrize("present", ["config.json", "weights.safetensors"])
def test_a_half_downloaded_model_is_not_cached(tmp_path, present):
    """The trap inside the trap: an interrupted fetch leaves the directory sitting there.

    A check for the directory alone would call this present and then fail inside the job — which is
    the exact failure mode the probe exists to move *out* of the job.
    """
    model = tmp_path / "m"
    model.mkdir()
    (model / present).write_bytes(b"\x00")
    assert not whisper.model_is_cached(str(model))
    assert whisper.resolve_model_dir(str(model)) is None


def test_an_absent_model_is_not_cached(tmp_path):
    assert not whisper.model_is_cached(str(tmp_path / "nope"))


def test_model_presence_never_reaches_the_network(monkeypatch, tmp_path):
    """`local_files_only=True` is the whole contract: the probe must stay local and free."""
    seen: dict = {}

    def fake_snapshot_download(**kwargs):
        seen.update(kwargs)
        return str(tmp_path)

    module = types.ModuleType("huggingface_hub")
    module.snapshot_download = fake_snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)

    whisper.resolve_model_dir("mlx-community/whisper-large-v3-turbo")
    assert seen["local_files_only"] is True


# ------------------------------------------------------------------------- ffmpeg
def test_ffmpeg_is_put_on_path_because_mlx_hardcodes_the_bare_name(monkeypatch, tmp_path):
    """mlx_whisper.audio.load_audio runs `["ffmpeg", ...]` — there is no path to hand it.

    So resolving it is not enough; its directory has to be on PATH before mlx_whisper shells out,
    or a Dock-launched app (launchd's minimal PATH) cannot decode audio at all.
    """
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(whisper, "resolve_ffmpeg", lambda executable="ffmpeg": "/opt/homebrew/bin/ffmpeg")

    whisper._ffmpeg_on_path()

    assert "/opt/homebrew/bin" in os.environ["PATH"].split(os.pathsep)


def test_putting_ffmpeg_on_path_is_idempotent(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(whisper, "resolve_ffmpeg", lambda executable="ffmpeg": "/opt/homebrew/bin/ffmpeg")

    whisper._ffmpeg_on_path()
    whisper._ffmpeg_on_path()

    assert os.environ["PATH"].split(os.pathsep).count("/opt/homebrew/bin") == 1


def test_a_missing_ffmpeg_is_a_named_failure(monkeypatch, audio, cached_model, fake_mlx):
    monkeypatch.setattr(whisper, "resolve_ffmpeg", lambda executable="ffmpeg": None)
    with pytest.raises(whisper.TranscriptionError, match="ffmpeg"):
        whisper.transcribe(audio)


# --------------------------------------------------------------------- transcribe
def test_transcribe_returns_text_and_segments(audio, cached_model, fake_mlx):
    result = whisper.transcribe(audio)

    assert result.text == "안녕하세요. 회의를 시작합니다."
    assert result.language == "ko"
    assert [s.text for s in result.segments] == ["안녕하세요.", "회의를 시작합니다."]
    assert result.segments[0].start == 0.0
    assert not result.is_empty


def test_transcribe_passes_the_resolved_directory_not_the_repo_id(audio, cached_model, fake_mlx):
    """The offline fix, pinned.

    `load_model` only calls snapshot_download when `Path(path_or_hf_repo).exists()` is false — and
    snapshot_download phones HuggingFace to resolve `main` **even for a fully cached model**
    (measured: three requests to huggingface.co on a cached transcription). Passing the resolved
    directory takes that branch out entirely. Passing the repo id here would restore the network
    call, and nothing else in the suite would notice.
    """
    whisper.transcribe(audio)

    assert fake_mlx[0]["path_or_hf_repo"] == str(cached_model)
    assert fake_mlx[0]["path_or_hf_repo"] != whisper.DEFAULT_MODEL


def test_transcribe_refuses_rather_than_downloading(audio, monkeypatch, fake_mlx):
    """The headline rule: a 1.5GB fetch never happens inside a job the owner is waiting on."""
    monkeypatch.setattr(whisper, "resolve_model_dir", lambda m=whisper.DEFAULT_MODEL: None)

    with pytest.raises(whisper.TranscriptionError) as exc:
        whisper.transcribe(audio)

    assert "download_model.py" in str(exc.value)
    assert not fake_mlx, "it must not reach mlx_whisper at all"


def test_transcribe_passes_the_language_through(audio, cached_model, fake_mlx):
    whisper.transcribe(audio, language="en")
    assert fake_mlx[0]["language"] == "en"


def test_a_missing_audio_file_is_a_named_failure(tmp_path, cached_model, fake_mlx):
    with pytest.raises(whisper.TranscriptionError, match="오디오 파일이 없습니다"):
        whisper.transcribe(tmp_path / "gone.m4a")


def test_an_mlx_failure_becomes_a_transcription_error(audio, cached_model, monkeypatch):
    """ffmpeg failures surface from mlx as RuntimeError three frames down; type them here."""
    module = types.ModuleType("mlx_whisper")

    def boom(*a, **kw):
        raise RuntimeError("Failed to load audio: moov atom not found")

    module.transcribe = boom
    monkeypatch.setitem(sys.modules, "mlx_whisper", module)

    with pytest.raises(whisper.TranscriptionError, match="moov atom"):
        whisper.transcribe(audio)


def test_a_missing_package_is_a_named_failure(audio, cached_model, monkeypatch):
    monkeypatch.setitem(sys.modules, "mlx_whisper", None)
    with pytest.raises(whisper.TranscriptionError, match="mlx-whisper"):
        whisper.transcribe(audio)


def test_silence_transcribes_to_an_empty_transcript(audio, cached_model, monkeypatch):
    module = types.ModuleType("mlx_whisper")
    module.transcribe = lambda *a, **kw: {"text": "   ", "language": "ko", "segments": []}
    monkeypatch.setitem(sys.modules, "mlx_whisper", module)

    assert whisper.transcribe(audio).is_empty


# ---------------------------------------------------------------------- rendering
def test_format_timestamp():
    assert whisper.format_timestamp(0) == "00:00"
    assert whisper.format_timestamp(75) == "01:15"
    assert whisper.format_timestamp(3671) == "1:01:11"


def test_rendered_transcript_carries_timestamps(audio, cached_model, fake_mlx):
    """The timestamps are what let the review block point the owner at the recording."""
    result = whisper.transcribe(audio)

    text = whisper.render_transcript(result, source_name="meeting.m4a")

    assert "[00:00 -> 00:03] 안녕하세요." in text
    assert "[00:03 -> 00:07] 회의를 시작합니다." in text
    assert "# Transcript: meeting.m4a" in text


def test_rendered_transcript_keeps_text_when_there_are_no_segments():
    """A header-only file would silently hand the model an empty transcript."""
    transcript = whisper.Transcript(text="한 마디", segments=[])
    assert "한 마디" in whisper.render_transcript(transcript, source_name="x.m4a")
