from pathlib import Path

from contextbot.files.originals import embed_link, move_into_assets, move_to_downloads


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


# ------------------------------------------------------- keep-and-embed (images)
def test_move_into_assets(tmp_path: Path):
    src = _file(tmp_path / "tmp", "shot.png", "png bytes")
    assets = tmp_path / "vault" / ".assets"

    stored = move_into_assets(src, assets_dir=assets)

    assert stored == assets / "shot.png"
    assert stored.read_text(encoding="utf-8") == "png bytes"
    assert not src.exists()


def test_move_into_assets_renames_to_match_the_note(tmp_path: Path):
    src = _file(tmp_path / "tmp", "IMG_4821.png")
    stored = move_into_assets(
        src, assets_dir=tmp_path / ".assets", filename="260715-1430-예산_검토.png"
    )
    assert stored.name == "260715-1430-예산_검토.png"


def test_move_into_assets_never_overwrites(tmp_path: Path):
    assets = tmp_path / ".assets"
    _file(assets, "shot.png", "first")

    stored = move_into_assets(_file(tmp_path / "tmp", "shot.png", "second"), assets_dir=assets)

    assert stored.name == "shot-2.png"
    assert (assets / "shot.png").read_text(encoding="utf-8") == "first"


def test_embed_link_is_vault_root_relative(tmp_path: Path):
    """MarkNotes resolves `.assets/x` against the vault root, not the note's folder — which is
    what keeps the embed working after the note is triaged out of 0_inbox."""
    assert embed_link(Path("/vault/.assets/260715-1430-shot.png")) == (
        "![260715-1430-shot](.assets/260715-1430-shot.png)"
    )
