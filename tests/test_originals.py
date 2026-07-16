from pathlib import Path

from contextbot.files.originals import move_to_downloads


def _file(directory: Path, name: str, content: str = "x") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


def test_move_to_downloads(tmp_path: Path):
    src = _file(tmp_path / "tmp", "report.pdf", "pdf bytes")
    downloads = tmp_path / "Downloads"

    moved = move_to_downloads(src, downloads_dir=downloads)

    assert moved == downloads / "report.pdf"
    assert moved.read_text(encoding="utf-8") == "pdf bytes"
    assert not src.exists()


def test_creates_the_downloads_dir(tmp_path: Path):
    src = _file(tmp_path / "tmp", "a.pdf")
    moved = move_to_downloads(src, downloads_dir=tmp_path / "nested" / "Downloads")
    assert moved.exists()


def test_collision_never_overwrites(tmp_path: Path):
    """Re-sending a file must not clobber the copy already in Downloads."""
    downloads = tmp_path / "Downloads"
    _file(downloads, "report.pdf", "original")

    src = _file(tmp_path / "tmp", "report.pdf", "resent")
    moved = move_to_downloads(src, downloads_dir=downloads)

    assert moved.name == "report-2.pdf"
    assert (downloads / "report.pdf").read_text(encoding="utf-8") == "original"
    assert moved.read_text(encoding="utf-8") == "resent"


def test_expands_user_relative_dirs(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    moved = move_to_downloads(_file(tmp_path / "tmp", "a.pdf"), downloads_dir="~/Downloads")
    assert moved == tmp_path / "Downloads" / "a.pdf"
