#!/usr/bin/env python3
"""Fetch the Whisper weights, deliberately and visibly.

Usage: .venv/bin/python scripts/download_model.py [--model <hf-repo-id>] [--check]

This step exists because nothing else creates it. `mlx_whisper` takes a HuggingFace *repo id*, not
a file, and downloads ~1.5GB on first use if it is not cached — so without this, "installing the
model" is a side effect of somebody's first meeting note: a silent multi-minute stall mid-job, and
an outright failure with no network. Run this once on a new machine (`--check` first to see whether
you need to). The health panel reports the same state, and the bot refuses to transcribe rather than
downloading behind your back.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from contextbot.stt import whisper  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Download the Whisper model used for meeting notes")
    parser.add_argument("--model", default=whisper.DEFAULT_MODEL, help="HuggingFace repo id")
    parser.add_argument(
        "--check", action="store_true", help="report whether it is cached; download nothing"
    )
    args = parser.parse_args()

    if not whisper.package_is_installed():
        print("❌ mlx-whisper is not installed — run: pip install -r requirements.txt")
        return 1

    if whisper.model_is_cached(args.model):
        print(f"✅ already downloaded: {args.model}")
        return 0
    if args.check:
        print(f"❌ not downloaded: {args.model} ({whisper.MODEL_SIZE_HINT})")
        print("   run this script without --check to fetch it")
        return 1

    print(f"⬇️  downloading {args.model} ({whisper.MODEL_SIZE_HINT}) — this takes a few minutes…")
    try:
        path = whisper.download_model(args.model)
    except whisper.TranscriptionError as exc:
        print(f"❌ {exc}")
        return 1
    print(f"✅ done: {path}")

    ffmpeg = whisper.resolve_ffmpeg()
    if ffmpeg is None:
        # Not fatal to the download, but the next transcription needs it, so say so now.
        print("⚠️  ffmpeg not found — audio cannot be decoded. Install it: brew install ffmpeg")
        return 1
    print(f"✅ ffmpeg: {ffmpeg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
