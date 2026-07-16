"""A label that ends in an ellipsis instead of clipping mid-character."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QLabel, QWidget


class ElidedLabel(QLabel):
    """A :class:`QLabel` that shortens its text to fit, with a `…`, and keeps the original.

    Qt does not do this on its own: a `QLabel` too narrow for its text simply stops drawing where
    the pixels run out — mid-character — which reads as a rendering bug rather than as "there is
    more text here".

    **The full string is kept**, for two reasons that are both easy to get wrong:

    - The visible text is a lopped-off copy, so eliding *that* on the next resize would eat the
      string a word at a time until nothing was left. Every elision runs from the original.
    - Widening the label has to restore what it cut, which is only possible if it still exists.

    The full text is also the tooltip, so nothing shortened here is actually unreachable.

    **The size policy is the caller's, deliberately**, and getting that wrong is subtle enough to
    be worth the warning. This class used to force `Ignored` horizontally, which a *stretchy* label
    does need (a `QLabel`'s minimum width is its whole text, so any other policy makes it refuse to
    shrink and shove its neighbours off the row instead of eliding). But `Ignored` combined with a
    caller's :meth:`setFixedWidth` is silently broken: `setFixedWidth` pins the widget's min and max
    but **does not touch the policy**, so the layout keeps sizing the column as if the label were
    zero-width — it hands the space to somebody else and then draws this label, at its real width,
    straight over the neighbour it just placed. Measured: a 150px label positioned at x=547 in a
    592px row, with the next widget laid out at 557 underneath it.

    So: a **fixed-width** caller wants the default policy, and a **stretchy** one wants `Ignored`
    plus an explicit :meth:`setMinimumWidth` for the layout to reserve.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._full = ""
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt naming)
        self._full = text or ""
        self.setToolTip(self._full)
        self._apply()

    def full_text(self) -> str:
        """The unshortened text. :meth:`text` returns whatever currently fits."""
        return self._full

    def _apply(self) -> None:
        width = self.width()
        shown = (
            QFontMetrics(self.font()).elidedText(self._full, Qt.ElideRight, width)
            if width > 0
            else self._full
        )
        QLabel.setText(self, shown)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().resizeEvent(event)
        self._apply()
