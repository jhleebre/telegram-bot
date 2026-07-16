"""Image handler tests: a sent image → a note that embeds it, described and transcribed.

No model is ever run and no image is ever converted for real: the engine is a fake returning
canned text, and downloads write bytes to the path the handler chose — which is what lets the
staging isolation be asserted as the model would actually see it.
"""

import base64
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

PNG_BYTES = b"\x89PNG\r\n\x1a\nnot a real png, but these exact bytes must survive the round trip"
JPEG_BYTES = b"\xff\xd8\xff\xe0jpeg-ish"

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


def _embedded_bytes(body: str) -> bytes:
    """Decode the image back out of the note's data URL — the note *is* the storage now."""
    encoded = body.split(";base64,", 1)[1].split(")", 1)[0]
    return base64.b64decode(encoded)


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
        # Mirror the real target mapping: a heic is a camera photo and becomes a JPEG, not a PNG.
        suffix = ".png" if images.TRANSCODE_TARGETS.get(src.suffix.lower()) == "png" else ".jpg"
        dest = Path(dest_dir) / f"{src.stem}{suffix}"
        dest.write_bytes(JPEG_BYTES if suffix == ".jpg" else PNG_BYTES)
        src.unlink()  # sips leaves the original; the handler stages a temp dir either way
        return dest

    monkeypatch.setattr("contextbot.handlers.image_handler.normalize", _convert)


# =================================================================== the happy path
async def test_image_note_embeds_the_image_and_the_description(llm_settings):
    result = await handle_image(_msg("shot.png", PNG_BYTES), llm_settings, engine=FakeEngine())

    assert result.saved_path is not None
    body = _body(result.saved_path)
    # The embed comes first, and carries the image's actual bytes.
    assert body.strip().startswith("![3분기_인프라_예산_검토_화면](data:image/png;base64,")
    assert _embedded_bytes(body) == PNG_BYTES
    assert "2026 3분기 인프라 예산 검토" in body
    assert "## 텍스트" in body


async def test_the_note_is_the_only_thing_written(llm_settings):
    """The image goes *inside* the note, so nothing is left beside it: no .assets file to keep in
    sync with MarkNotes' .metadata.json ledger, and nothing filed to Downloads."""
    await handle_image(_msg("shot.png"), llm_settings, engine=FakeEngine())

    assert len(list(llm_settings.inbox_dir.iterdir())) == 1
    vault_root = llm_settings.inbox_dir.parent
    assert not (vault_root / ".assets").exists()
    assert not llm_settings.downloads_dir.exists()


async def test_the_mime_type_matches_the_format(llm_settings):
    """MarkNotes renders the data URL by its declared MIME type, so a jpeg must not claim png."""
    result = await handle_image(
        _msg(None, JPEG_BYTES, kind=MessageKind.IMAGE, auto_ext=".jpg"),
        llm_settings,
        engine=FakeEngine(),
    )

    assert "(data:image/jpeg;base64," in _body(result.saved_path)


async def test_title_comes_from_the_model_not_the_filename(llm_settings):
    """`IMG_4821` is noise; the model's title is what the owner can search for."""
    result = await handle_image(_msg("IMG_4821.png"), llm_settings, engine=FakeEngine())

    assert _fm(result.saved_path)["title"] == "3분기 인프라 예산 검토 화면"
    assert result.saved_path.name == "260715-노트-3분기_인프라_예산_검토_화면.md"


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


async def test_frontmatter_records_the_source(llm_settings):
    result = await handle_image(
        _msg("shot.png", message_id=31), llm_settings, engine=FakeEngine()
    )

    fm = _fm(result.saved_path)
    assert fm["telegram_message_id"] == 31
    assert fm["type"] == "image"
    assert fm["original_file"] == "shot.png"
    # No `image:` key: there is no file to point at, so a path there could only ever be a lie.
    assert "image" not in fm


async def test_a_photo_records_no_original_filename(llm_settings):
    """A Telegram photo has no name; recording the temp name we invented would say nothing."""
    result = await handle_image(
        _msg(None, kind=MessageKind.IMAGE, auto_ext=".jpg"), llm_settings, engine=FakeEngine()
    )

    assert "original_file" not in _fm(result.saved_path)


async def test_a_telegram_photo_has_no_filename_and_still_works(llm_settings):
    """A Telegram *photo* carries no file name; Telethon supplies the `.jpg` on download.

    The photo is the single most common input, so it must reach the model as a readable image and
    land in the note as one — with no conversion, since `.jpg` already is one.
    """
    engine = FakeEngine()
    result = await handle_image(
        _msg(None, JPEG_BYTES, kind=MessageKind.IMAGE, auto_ext=".jpg"),
        llm_settings,
        engine=engine,
    )

    assert result.saved_path is not None
    assert _embedded_bytes(_body(result.saved_path)) == JPEG_BYTES
    # It was staged and described as a real image, not silently degraded to a stub.
    assert engine.calls[0]["visible"] == ["telegram-7.jpg"]
    assert "설명을 생성하지 못했습니다" not in _body(result.saved_path)


async def test_heic_is_converted_before_the_model_and_before_the_note(llm_settings, fake_sips):
    """The iPhone case, and the reason files/images.py exists.

    `Read` does not render a heic — it returns raw bytes and the model describes the *file header*
    while reporting success. MarkNotes cannot render one either. So the converted image is what the
    model sees *and* what the note embeds; the heic must not survive into either.
    """
    engine = FakeEngine()
    result = await handle_image(_msg("IMG_4821.heic"), llm_settings, engine=engine)

    assert engine.calls[0]["visible"] == ["IMG_4821.jpg"]  # the heic never reaches the model
    body = _body(result.saved_path)
    assert "(data:image/jpeg;base64," in body
    assert b"heic" not in _embedded_bytes(body)


async def test_a_readable_image_is_never_re_encoded(llm_settings, fake_sips):
    """Re-encoding a PNG would cost quality and inflate the note that carries it."""
    result = await handle_image(_msg("shot.png", PNG_BYTES), llm_settings, engine=FakeEngine())

    assert _embedded_bytes(_body(result.saved_path)) == PNG_BYTES


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
        _msg("shot.png", PNG_BYTES), llm_settings, engine=FakeEngine(text="DESCRIPTION_FAILED")
    )

    assert result.saved_path is not None
    body = _body(result.saved_path)
    assert _embedded_bytes(body) == PNG_BYTES  # the capture survives in full
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
        _msg("shot.png", PNG_BYTES),
        llm_settings,
        engine=FakeEngine(raises=ClaudeTimeout("30s 초과")),
    )

    assert _embedded_bytes(_body(result.saved_path)) == PNG_BYTES
    assert "설명 없이 저장했습니다" in result.reply
    assert "30s 초과" in _body(result.saved_path)


async def test_claude_disabled_saves_the_image_without_calling_the_engine(settings):
    engine = FakeEngine()
    result = await handle_image(_msg("shot.png", PNG_BYTES), settings, engine=engine)

    assert engine.calls == []
    assert _embedded_bytes(_body(result.saved_path)) == PNG_BYTES
    assert "CLAUDE_ENABLED=false" in _body(result.saved_path)


# --- the two cases where there is nothing embeddable, so there is no note to write
async def test_an_image_too_big_to_embed_is_filed_to_downloads_instead(llm_settings):
    """The image *is* the note now, so one that cannot go in leaves nothing worth writing —
    but the file itself is still handed back rather than dropped."""
    engine = FakeEngine()
    result = await handle_image(_msg("huge.png", b"x" * 11_000_000), llm_settings, engine=engine)

    assert engine.calls == []  # never sent to the model: we already know it cannot be embedded
    assert result.saved_path is None
    assert list(llm_settings.inbox_dir.iterdir()) == []
    assert (llm_settings.downloads_dir / "huge.png").is_file()
    assert "너무 커서" in result.reply and "Downloads" in result.reply


async def test_the_size_cap_applies_after_conversion_not_before(llm_settings, monkeypatch):
    """A 1.85MB .heic becomes a 4MB JPEG — and would become an 18MB PNG. Only the converted size
    predicts the note's size, so checking the original's would let a monster through."""

    async def _explode(src, dest_dir):
        dest = Path(dest_dir) / f"{src.stem}.jpg"
        dest.write_bytes(b"x" * 11_000_000)
        return dest

    monkeypatch.setattr("contextbot.handlers.image_handler.normalize", _explode)

    result = await handle_image(_msg("IMG.heic", b"small"), llm_settings, engine=FakeEngine())

    assert result.saved_path is None  # caught, despite the original being 5 bytes
    assert (llm_settings.downloads_dir / "IMG.heic").is_file()


async def test_an_unconvertible_image_is_filed_to_downloads_instead(llm_settings, no_sips):
    engine = FakeEngine()
    result = await handle_image(_msg("photo.heic"), llm_settings, engine=engine)

    assert engine.calls == []
    assert result.saved_path is None
    assert "변환 실패" in result.reply
    assert (llm_settings.downloads_dir / "photo.heic").is_file()


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

    assert list(llm_settings.inbox_dir.iterdir()) == []
    assert not llm_settings.downloads_dir.exists()


# =================================================================== collisions
async def test_two_images_never_overwrite_each_other(llm_settings):
    first = await handle_image(_msg("shot.png", message_id=1), llm_settings, engine=FakeEngine())
    second = await handle_image(_msg("shot.png", message_id=2), llm_settings, engine=FakeEngine())

    assert first.saved_path != second.saved_path
    assert len(list(llm_settings.inbox_dir.iterdir())) == 2
