"""Original-file policy: what becomes of the file the owner sent, once its note exists.

Per pipeline (docs/PHASE2.md, "Original-file policy"):

===========  ====================================================  ===========
Input        Policy                                                Increment
===========  ====================================================  ===========
pdf/txt/csv  moved to ``~/Downloads/``                             2
markdown     *is* the note — saved to the inbox, never moved       2
image        kept, embedded in the note                            3
audio        deleted after success                                 5
===========  ====================================================  ===========

**Call these last.** A handler that hits a usage limit raises ``DeferMessage`` and is replayed
from scratch on the next Start, so any file move done before the last ``claude -p`` call would run
twice — the second time against a path that no longer exists.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..notes.naming import unique_path

logger = logging.getLogger("contextbot.files.originals")

DEFAULT_DOWNLOADS_DIR = "~/Downloads"

# The vault's image folder, and the link prefix that points at it. Both are MarkNotes' convention,
# read off its source rather than guessed: `ASSETS_PATH = <vault root>/.assets`, and an embed is
# matched by the literal pattern `![alt](.assets/<file>)`.
ASSETS_DIR_NAME = ".assets"


def move_to_downloads(src: Path, *, downloads_dir: Path | str | None = None) -> Path:
    """Move ``src`` into the downloads directory and return its final path.

    The name is kept, with ``-2``, ``-3``, … appended on collision, so re-sending a file never
    overwrites the copy already sitting in Downloads.
    """
    dest_dir = Path(downloads_dir or DEFAULT_DOWNLOADS_DIR).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)

    target = unique_path(dest_dir, src.name)
    # shutil.move, not Path.rename: the source is a temp file and may be on a different volume.
    shutil.move(str(src), str(target))
    logger.info("Moved original to %s", target)
    return target


def move_into_assets(src: Path, *, assets_dir: Path, filename: str | None = None) -> Path:
    """Move an image into the vault's ``.assets/`` and return its final path.

    The *keep-and-embed* policy (increment 3): unlike a document, an image is not filed away to
    ``~/Downloads`` — it moves **into the vault**, because the note embeds it and the embed has to
    resolve. The image is the note's content, not a leftover.

    ``filename`` renames it on the way in (the handler passes the note's own
    ``YYMMDD-HHMM-<slug>`` stem, so the image sorts and reads like the note it belongs to);
    collisions get ``-2``, ``-3``, … as everywhere else.
    """
    assets_dir.mkdir(parents=True, exist_ok=True)

    target = unique_path(assets_dir, filename or src.name)
    shutil.move(str(src), str(target))
    logger.info("Moved image into the vault at %s", target)
    return target


def embed_link(image_path: Path) -> str:
    """Return the Markdown embed for an image already sitting in the vault's ``.assets/``.

    Always ``![<name>](.assets/<file>)``. That prefix *looks* note-relative but is not: MarkNotes
    resolves it against the **vault root** (``path.join(ROOT_PATH, imagePath)``), so the link keeps
    working after the owner triages the note out of ``0_inbox`` into ``2_areas/…`` — which is the
    whole point of putting the image in one shared folder rather than beside the note.
    """
    return f"![{image_path.stem}]({ASSETS_DIR_NAME}/{image_path.name})"
