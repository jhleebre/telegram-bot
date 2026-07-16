"""Image handler tests: a sent image → a note that embeds it, described and transcribed.

No model is ever run and no image is ever converted for real: the engine is a fake returning
canned text, and downloads write bytes to the path the handler chose — which is what lets the
staging isolation be asserted as the model would actually see it.
"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from contextbot.core.router import classify_document
from contextbot.engine.claude_cli import ClaudeResult, ClaudeTimeout, ClaudeUsageLimit
from contextbot.files import images
from contextbot.handlers.base import DeferMessage, IncomingMessage, MessageKind
from contextbot.handlers.image_handler import handle_image

DATE = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)

DESCRIPTION = """제목: 3분기 인프라 예산 검토 화면

## 설명

2026년 3분기 인프라 예산 검토 화면의 스크린샷이다. 상단에 제목 배너가 있고 중앙에 분기별 비용 표가 있다.

## 텍스트

# 2026 3분기 인프라 예산 검토

| 항목 | 1분기 |
|------|-------|
| 서버 임대 | 12,400 |
"""


class FakeUpload:
    """Stands in for a Telethon Message with a downloadable attachment.

    Mirrors one real Telethon behaviour that matters here: when the target path has **no
    extension**, ``download_media`` appends the media's own (``_get_proper_filename``:
    ``if not ext: ext = extension``) and returns the adjusted path. That is why a Telegram *photo*,
    which carries no file name at all, still arrives as a ``.jpg`` the model can read.
    """

    def __init__(self, content: bytes, *, auto_ext: str = ""):
        self.content = content
        self.auto_ext = auto_ext
        self.downloads: list[str] = []

    async def download_media(self, file):
        target = Path(file)
        if not target.suffix and self.auto_ext:
            target = target.with_suffix(self.auto_ext)
        target.write_bytes(self.content)
        self.downloads.append(str(target))
        return str(target)


def _msg(
    file_name: str | None,
    content: bytes = b"\x89PNG\r\n\x1a\n",
    *,
    message_id: int = 7,
    kind: MessageKind | None = None,
    auto_ext: str = "",
) -> IncomingMessage:
    return IncomingMessage(
        user_id=42,
        chat_id=999,
        message_id=message_id,
        date=DATE,
        kind=kind or classify_document(file_name, None),
        file_name=file_name,
        raw=FakeUpload(content, auto_ext=auto_ext),
    )


class FakeEngine:
    """Stands in for ClaudeCLI: records each call, returns canned text or raises."""

    def __init__(self, *, text: str = DESCRIPTION, raises: Exception | None = None):
        self._text = text
        self._raises = raises
        self.calls: list[dict] = []

    async def run(self, prompt: str, **kwargs):
        call = {"prompt": prompt, **kwargs}
        cwd = kwargs.get("cwd")
        if cwd is not None:  # what the model could actually see, captured at call time
            call["visible"] = sorted(p.name for p in Path(cwd).iterdir())
        self.calls.append(call)
        if self._raises is not None:
            raise self._raises
        return ClaudeResult(text=self._text, session_id="s-1")


def _fm(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])


def _body(path: Path) -> str:
    return path.read_text(encoding="utf-8").split("---\n", 2)[2]


def _assets(settings) -> list[str]:
    d = settings.assets_dir
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


@pytest.fixture
def llm_settings(settings):
    return replace(settings, claude_enabled=True)


@pytest.fixture
def no_sips(monkeypatch):
    """Fail every transcode, without touching the real `sips`."""

    async def _boom(src, dest_dir):
        raise images.ImageError("변환 실패")

    monkeypatch.setattr("contextbot.handlers.image_handler.normalize", _boom)


@pytest.fixture
def fake_sips(monkeypatch):
    """Stand in for a successful `sips` conversion, so no test shells out to the real one."""

    async def _convert(src, dest_dir):
        if src.suffix.lower() in images.VISION_READABLE_EXTS:
            return src
        dest = Path(dest_dir) / f"{src.stem}.png"
        dest.write_bytes(b"\x89PNG\r\n\x1a\nconverted")
        src.unlink()  # sips leaves the original; the handler stages a temp dir either way
        return dest

    monkeypatch.setattr("contextbot.handlers.image_handler.normalize", _convert)


# =================================================================== the happy path
async def test_image_note_embeds_the_image_and_the_description(llm_settings):
    result = await handle_image(_msg("shot.png"), llm_settings, engine=FakeEngine())

    assert result.saved_path is not None
    body = _body(result.saved_path)
    stored = _assets(llm_settings)[0]
    # The embed comes first, and points at the file that actually exists in the vault.
    assert body.strip().startswith(f"![{Path(stored).stem}](.assets/{stored})")
    assert "2026 3분기 인프라 예산 검토" in body
    assert "## 텍스트" in body


async def test_image_is_kept_in_the_vault_not_moved_to_downloads(llm_settings):
    """Unlike every increment-2 route: the note embeds the image, so it lives in the vault."""
    await handle_image(_msg("shot.png"), llm_settings, engine=FakeEngine())

    assert len(_assets(llm_settings)) == 1
    downloads = llm_settings.downloads_dir
    assert not downloads.exists() or list(downloads.iterdir()) == []


async def test_embed_link_resolves_from_the_vault_root(llm_settings):
    """MarkNotes resolves `.assets/x` against the vault root, so the link must be exactly that —
    and the file must be there, or the note renders a broken image."""
    result = await handle_image(_msg("shot.png"), llm_settings, engine=FakeEngine())

    stored = _assets(llm_settings)[0]
    assert f"(.assets/{stored})" in _body(result.saved_path)
    vault_root = llm_settings.inbox_dir.parent
    assert (vault_root / ".assets" / stored).is_file()


async def test_image_is_named_after_its_note(llm_settings):
    result = await handle_image(_msg("shot.png"), llm_settings, engine=FakeEngine())

    stored = _assets(llm_settings)[0]
    assert stored.startswith("260715-1430-")
    assert stored.endswith(".png")
    # The note and its image sort together.
    assert result.saved_path.stem == Path(stored).stem


async def test_title_comes_from_the_model_not_the_filename(llm_settings):
    """`IMG_4821` is noise; the model's title is what the owner can search for."""
    result = await handle_image(_msg("IMG_4821.png"), llm_settings, engine=FakeEngine())

    assert _fm(result.saved_path)["title"] == "3분기 인프라 예산 검토 화면"
    assert result.saved_path.name == "260715-1430-3분기_인프라_예산_검토_화면.md"


async def test_the_title_line_is_not_repeated_in_the_body(llm_settings):
    """It is frontmatter, not content — the note should open with the image, then `## 설명`."""
    result = await handle_image(_msg("shot.png"), llm_settings, engine=FakeEngine())

    body = _body(result.saved_path)
    assert "제목:" not in body
    assert body.split("\n\n")[1].startswith("## 설명")


async def test_a_missing_title_line_falls_back_without_losing_the_description(llm_settings):
    """An older/looser reply is not a failure: the description is still perfectly good."""
    text = "## 설명\n\n스크린샷이다. 표가 있다.\n\n## 텍스트\n\n(텍스트 없음)"
    result = await handle_image(_msg("IMG_4821.png"), llm_settings, engine=FakeEngine(text=text))

    assert _fm(result.saved_path)["title"] == "IMG_4821"  # the filename, such as it is
    assert "## 설명" in _body(result.saved_path)
    assert "스크린샷이다" in _body(result.saved_path)


async def test_an_untitled_photo_with_no_title_line_still_gets_a_note(llm_settings):
    text = "## 설명\n\n창밖 풍경 사진이다.\n\n## 텍스트\n\n(텍스트 없음)"
    result = await handle_image(
        _msg(None, kind=MessageKind.IMAGE, auto_ext=".jpg"), llm_settings, engine=FakeEngine(text=text)
    )

    assert _fm(result.saved_path)["title"] == "이미지"


async def test_frontmatter_records_the_source_and_the_image(llm_settings):
    result = await handle_image(_msg("shot.png", message_id=31), llm_settings, engine=FakeEngine())

    fm = _fm(result.saved_path)
    assert fm["telegram_message_id"] == 31
    assert fm["type"] == "image"
    assert fm["image"] == f".assets/{_assets(llm_settings)[0]}"


async def test_a_telegram_photo_has_no_filename_and_still_works(llm_settings):
    """A Telegram *photo* carries no file name; Telethon supplies the `.jpg` on download.

    The photo is the single most common input, so it must reach the model as a readable image and
    land in the vault as one — with no conversion, since `.jpg` already is one.
    """
    engine = FakeEngine()
    result = await handle_image(
        _msg(None, kind=MessageKind.IMAGE, auto_ext=".jpg"), llm_settings, engine=engine
    )

    assert result.saved_path is not None
    assert _assets(llm_settings)[0].endswith(".jpg")
    # It was staged and described as a real image, not silently degraded to a stub.
    assert engine.calls[0]["visible"] == ["telegram-7.jpg"]
    assert "설명을 생성하지 못했습니다" not in _body(result.saved_path)


async def test_heic_is_converted_before_the_model_and_before_the_vault(llm_settings, fake_sips):
    """The iPhone case, and the reason files/images.py exists.

    `Read` does not render a heic — it returns raw bytes and the model describes the *file header*
    while reporting success. MarkNotes cannot render one either. So the PNG is what the model sees
    *and* what the note embeds; the heic must not survive into either.
    """
    engine = FakeEngine()
    result = await handle_image(_msg("IMG_4821.heic"), llm_settings, engine=engine)

    assert engine.calls[0]["visible"] == ["IMG_4821.png"]  # the heic never reaches the model
    stored = _assets(llm_settings)[0]
    assert stored.endswith(".png")
    assert f"(.assets/{stored})" in _body(result.saved_path)


async def test_a_readable_image_is_never_re_encoded(llm_settings, fake_sips):
    """Re-encoding a PNG would only cost quality and time — it is already what both readers want."""
    engine = FakeEngine()
    await handle_image(_msg("shot.png", b"\x89PNG\r\n\x1a\noriginal"), llm_settings, engine=engine)

    assert (llm_settings.assets_dir / _assets(llm_settings)[0]).read_bytes().endswith(b"original")


# =================================================================== isolation & prompt
async def test_image_is_staged_alone_in_an_isolated_dir(llm_settings):
    """The model describes a neighbour rather than admitting it cannot see its input, so the
    staging dir must hold the image and nothing else — and be the job's cwd."""
    engine = FakeEngine()
    await handle_image(_msg("shot.png"), llm_settings, engine=engine)

    call = engine.calls[0]
    stage = call["cwd"]
    assert call["visible"] == ["shot.png"]  # no neighbours to fabricate from
    assert list(call["add_dirs"]) == [stage]  # nothing else is readable
    assert str(stage / "shot.png") in call["prompt"]  # pointed at the staged copy


async def test_image_prompt_carries_the_path_and_the_sentinel(llm_settings):
    engine = FakeEngine()
    await handle_image(_msg("shot.png"), llm_settings, engine=engine)

    prompt = engine.calls[0]["prompt"]
    assert "shot.png" in prompt
    assert "DESCRIPTION_FAILED" in prompt


async def test_image_runs_on_the_image_model(llm_settings):
    """Measured: sonnet describes an image reliably, so this stays off the PDF route's opus."""
    engine = FakeEngine()
    await handle_image(_msg("shot.png"), replace(llm_settings, claude_image_model="haiku"), engine=engine)

    assert engine.calls[0]["model"] == "haiku"


# =================================================================== the sentinel
async def test_sentinel_writes_a_stub_note_that_still_embeds_the_image(llm_settings):
    """No description is recoverable — but the image is, and it is what the owner sent."""
    result = await handle_image(
        _msg("shot.png"), llm_settings, engine=FakeEngine(text="DESCRIPTION_FAILED")
    )

    assert result.saved_path is not None
    body = _body(result.saved_path)
    assert f"(.assets/{_assets(llm_settings)[0]})" in body
    assert "설명을 생성하지 못했습니다" in body
    assert "설명 없이 저장했습니다" in result.reply


async def test_sentinel_is_matched_on_the_first_line_not_by_substring(llm_settings):
    """A screenshot of an error table may legitimately contain the token."""
    text = DESCRIPTION + "\n에러 코드: DESCRIPTION_FAILED\n"
    result = await handle_image(_msg("shot.png"), llm_settings, engine=FakeEngine(text=text))

    assert "설명을 생성하지 못했습니다" not in _body(result.saved_path)
    assert "DESCRIPTION_FAILED" in _body(result.saved_path)  # transcribed as content


@pytest.mark.parametrize(
    "text",
    ["", "   ", "DESCRIPTION_FAILED", "`DESCRIPTION_FAILED`", "# DESCRIPTION_FAILED",
     "DESCRIPTION_FAILED: 읽을 수 없습니다", "## 설명"],
)
async def test_unusable_replies_all_degrade_to_a_stub(llm_settings, text):
    result = await handle_image(_msg("shot.png"), llm_settings, engine=FakeEngine(text=text))

    assert "설명을 생성하지 못했습니다" in _body(result.saved_path)


# =================================================================== degradation
async def test_engine_failure_still_saves_the_image(llm_settings):
    result = await handle_image(
        _msg("shot.png"), llm_settings, engine=FakeEngine(raises=ClaudeTimeout("30s 초과"))
    )

    assert len(_assets(llm_settings)) == 1
    assert "설명 없이 저장했습니다" in result.reply
    assert "30s 초과" in _body(result.saved_path)


async def test_claude_disabled_saves_the_image_without_calling_the_engine(settings):
    engine = FakeEngine()
    result = await handle_image(_msg("shot.png"), settings, engine=engine)

    assert engine.calls == []
    assert len(_assets(settings)) == 1
    assert "CLAUDE_ENABLED=false" in _body(result.saved_path)


async def test_oversized_image_is_kept_but_not_described(llm_settings):
    engine = FakeEngine()
    result = await handle_image(_msg("huge.png", b"x" * 11_000_000), llm_settings, engine=engine)

    assert engine.calls == []  # never sent to the model
    assert len(_assets(llm_settings)) == 1  # but still kept and embedded
    assert "너무 큽니다" in _body(result.saved_path)


async def test_unconvertible_image_is_kept_but_not_described(llm_settings, no_sips):
    engine = FakeEngine()
    result = await handle_image(_msg("photo.heic"), llm_settings, engine=engine)

    assert engine.calls == []
    assert "변환 실패" in _body(result.saved_path)
    assert len(_assets(llm_settings)) == 1


# =================================================================== the deferral rule
async def test_usage_limit_defers(llm_settings):
    with pytest.raises(DeferMessage):
        await handle_image(
            _msg("shot.png"), llm_settings, engine=FakeEngine(raises=ClaudeUsageLimit("limit"))
        )


async def test_usage_limit_leaves_no_side_effect(llm_settings):
    """The replay re-runs the handler from scratch, so a deferral must write and move nothing."""
    with pytest.raises(DeferMessage):
        await handle_image(
            _msg("shot.png"), llm_settings, engine=FakeEngine(raises=ClaudeUsageLimit("limit"))
        )

    assert _assets(llm_settings) == []
    assert list(llm_settings.inbox_dir.iterdir()) == []


# =================================================================== collisions
async def test_two_images_never_overwrite_each_other(llm_settings):
    first = await handle_image(_msg("shot.png", message_id=1), llm_settings, engine=FakeEngine())
    second = await handle_image(_msg("shot.png", message_id=2), llm_settings, engine=FakeEngine())

    assert first.saved_path != second.saved_path
    assert len(_assets(llm_settings)) == 2
    # Each note points at its *own* image, not the other's.
    for result in (first, second):
        stored = _body(result.saved_path).split("(.assets/")[1].split(")")[0]
        assert (llm_settings.assets_dir / stored).is_file()
