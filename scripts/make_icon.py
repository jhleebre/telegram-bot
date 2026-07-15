#!/usr/bin/env python3
"""Render the app icon: a rounded green tile with the 🤖 emoji.

Usage: python scripts/make_icon.py <output.png>
Run with QT_QPA_PLATFORM=offscreen so it works without a display.
"""

import sys

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath

SIZE = 1024


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "icon.png"
    app = QGuiApplication(sys.argv)  # noqa: F841 - needed for font/text rendering

    img = QImage(SIZE, SIZE, QImage.Format_ARGB32)
    img.fill(Qt.transparent)

    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing)

    # Rounded tile with a soft green gradient (macOS-style squircle-ish corners).
    path = QPainterPath()
    margin = 40
    radius = 210
    path.addRoundedRect(QRectF(margin, margin, SIZE - 2 * margin, SIZE - 2 * margin), radius, radius)
    gradient = QLinearGradient(0, margin, 0, SIZE - margin)
    gradient.setColorAt(0.0, QColor("#37d67a"))
    gradient.setColorAt(1.0, QColor("#23b565"))
    painter.fillPath(path, gradient)

    # Robot emoji, centered.
    font = QFont()
    font.setPointSize(520)
    painter.setFont(font)
    painter.setPen(QColor("#ffffff"))
    painter.drawText(img.rect(), Qt.AlignCenter, "🤖")
    painter.end()

    if not img.save(out):
        print(f"failed to save {out}", file=sys.stderr)
        return 1
    print(f"icon saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
