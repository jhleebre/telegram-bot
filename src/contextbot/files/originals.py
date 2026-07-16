"""Original-file policy: what becomes of the file the owner sent, once its note exists.

Per pipeline (docs/PHASE2.md, "Original-file policy"):

===========  ====================================================  ===========
Input        Policy                                                Increment
===========  ====================================================  ===========
pdf/txt/csv  moved to ``~/Downloads/``                             2
markdown     *is* the note — saved to the inbox, never moved       2
image        embedded *into* the note (base64); no file kept       3
audio        deleted after success                                 5
===========  ====================================================  ===========

The image case needs nothing from this module: ``files/images.to_data_url`` puts the bytes inside
the note itself, so there is no leftover file to file away. An image too large to embed is the one
exception, and it falls back to ``move_to_downloads`` like any other original.

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
