"""Small custom widgets."""

from __future__ import annotations

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtWidgets import (
    QPlainTextEdit,
    QSlider,
    QStyle,
    QStyleOptionSlider,
)

from kokoro_editor.characters import CHARACTER_MIME_TYPE


class EditorTextEdit(QPlainTextEdit):
    """Plain text editor that accepts characters dragged from the panel,
    reporting them with character_dropped(name) once the cursor sits at
    the drop position."""

    character_dropped = Signal(str)

    def canInsertFromMimeData(self, source: QMimeData) -> bool:
        return source.hasFormat(
            CHARACTER_MIME_TYPE
        ) or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source: QMimeData) -> None:
        if source.hasFormat(CHARACTER_MIME_TYPE):
            name = bytes(source.data(CHARACTER_MIME_TYPE).data()).decode()
            self.character_dropped.emit(name)
        else:
            super().insertFromMimeData(source)


class SeekSlider(QSlider):
    """Horizontal slider where clicking the groove jumps straight to that spot
    (and lets you keep dragging) instead of paging by a fixed step."""

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > 0:
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            style = self.style()
            groove = style.subControlRect(
                QStyle.ComplexControl.CC_Slider,
                option,
                QStyle.SubControl.SC_SliderGroove,
                self,
            )
            handle = style.subControlRect(
                QStyle.ComplexControl.CC_Slider,
                option,
                QStyle.SubControl.SC_SliderHandle,
                self,
            )
            pos = event.position().toPoint()
            if not handle.contains(pos):
                span = max(groove.width() - handle.width(), 1)
                x = pos.x() - groove.x() - handle.width() // 2
                value = QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(), x, span, option.upsideDown
                )
                self.setValue(
                    value
                )  # handle now sits under the cursor, so the drag starts there
        super().mousePressEvent(event)
