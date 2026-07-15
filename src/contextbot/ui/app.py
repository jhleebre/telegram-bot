"""QApplication bootstrap: load settings, wire the worker and window, run the event loop."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from ..config import ConfigError, Settings
from ..logging_setup import configure_logging
from .bot_worker import BotWorker
from .main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    # Fusion gives consistent, predictable rendering of our custom QSS across themes.
    app.setStyle("Fusion")

    try:
        settings = Settings.load()
    except ConfigError as exc:
        QMessageBox.critical(
            None,
            "설정 오류",
            f"{exc}\n\n프로젝트 폴더의 .env 파일을 설정한 뒤 다시 실행해주세요.\n"
            "(.env.example 참고)",
        )
        return 1

    worker = BotWorker(settings)
    configure_logging(settings.log_level, ui_callback=worker.log_line.emit)

    window = MainWindow(worker)
    worker.start()  # start the QThread hosting the asyncio loop
    window.show()

    exit_code = app.exec()
    worker.shutdown()
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
