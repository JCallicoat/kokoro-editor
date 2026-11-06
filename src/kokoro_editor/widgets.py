"""Small custom widgets."""

from __future__ import annotations

import html
import re
from collections.abc import Callable

import shiboken6
from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QMimeData,
    QObject,
    QPoint,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDrag,
    QDragLeaveEvent,
    QDropEvent,
    QGuiApplication,
    QHelpEvent,
    QMouseEvent,
    QTextBlock,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QColorDialog,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QToolTip,
    QWidget,
)

from kokoro_editor.characters import CHARACTER_MIME_TYPE
from kokoro_editor.colors import text_color
from kokoro_editor.tags import string_index, tag_at, utf16_index


def shift_held() -> bool:
    """Whether Shift is down now (not as of the last input event)."""
    return bool(
        QGuiApplication.queryKeyboardModifiers()
        & Qt.KeyboardModifier.ShiftModifier
    )


def rich_tooltip(text: str) -> str:
    """Plain text as a tooltip that wraps, as a long description needs: Qt
    only wraps rich text. Blank lines separate paragraphs."""
    paragraphs = re.split(r"\n\s*\n", text.strip())
    return "".join(
        "<p>" + html.escape(p).replace("\n", "<br>") + "</p>"
        for p in paragraphs
    )


class ShiftTooltips(QObject):
    """Application-wide detailed tooltips: while Shift is held, a widget's
    What's This text (or an item view item's) shows instead of its tooltip.

    Widgets that make their own tooltips, handling QEvent.ToolTip, check
    shift_held() themselves. Either way the tooltip showing is asked for
    again whenever Shift is pressed or released.
    """

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        # The last tooltip asked for: widget, local and global position.
        self._last: tuple[QWidget, QPoint, QPoint] | None = None
        self._last_key_event = None
        app.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind == QEvent.Type.ToolTip and isinstance(watched, QWidget):
            self._last = (watched, event.pos(), event.globalPos())
            text = self._whats_this(watched, event.pos())
            if text and shift_held():
                QToolTip.showText(
                    event.globalPos(), rich_tooltip(text), watched
                )
                return True
        elif (
            kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
            and event.key() == Qt.Key.Key_Shift
            and not event.isAutoRepeat()
            # A key event reaches the filter again for each parent it's
            # passed on to.
            and event is not self._last_key_event
        ):
            self._last_key_event = event
            self._ask_again()
        return False

    def _ask_again(self) -> None:
        if self._last is None or not QToolTip.isVisible():
            return
        widget, pos, global_pos = self._last
        if shiboken6.isValid(widget):
            QApplication.sendEvent(
                widget, QHelpEvent(QEvent.Type.ToolTip, pos, global_pos)
            )

    @staticmethod
    def _whats_this(widget: QWidget, pos: QPoint) -> str:
        view = widget.parent()
        if isinstance(view, QAbstractItemView) and widget is view.viewport():
            index = view.indexAt(pos)
            if not index.isValid():
                return ""
            return index.data(Qt.ItemDataRole.WhatsThisRole) or ""
        return widget.whatsThis()


# Drag and drop format for a tag moved within (or copied from) the editor.
TAG_MIME_TYPE = "application/x-kokoro-editor-tag"
# Drag and drop format for a kind of tag to ask for, such as "pause",
# dragged from the tag toolbar.
NEW_TAG_MIME_TYPE = "application/x-kokoro-editor-new-tag"


class ColorButton(QPushButton):
    """A button showing a colour (and its hex code, in readable text) that
    opens a colour picker when clicked."""

    color_changed = Signal(str)  # "#rrggbb"

    def __init__(self, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(90)
        self.clicked.connect(self._choose)
        self.set_color(color)

    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        self._color = color.lower()
        self.setText(self._color)
        self.setStyleSheet(
            f"background-color: {self._color}; color: {text_color(color)};"
        )
        self.color_changed.emit(self._color)

    def _choose(self) -> None:
        chosen = QColorDialog.getColor(
            QColor(self._color), self.window(), "Choose a Colour"
        )
        if chosen.isValid():
            self.set_color(chosen.name())


class EditorTextEdit(QPlainTextEdit):
    """Plain text editor that understands tags:

    - characters dragged from the panel are reported with
      character_dropped(name) once the cursor sits at the drop position;
    - so are kinds of tag dragged from the tag toolbar, with
      new_tag_dropped(kind);
    - a tag can be dragged to move it (Ctrl copies), or to other apps;
    - double-clicking a tag reports it with tag_double_clicked(start, end)
      in document positions;
    - hovering asks tag_tooltip(block text, index) what to show.

    Tags are found by their own characters, never by spaces around them.
    """

    character_dropped = Signal(str)
    new_tag_dropped = Signal(str)  # "pause", "rate" or "pronunciation"
    tag_double_clicked = Signal(int, int)  # document positions

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tag_tooltip: Callable[[str, int], str | None] = (
            lambda _text, _index: None
        )
        self._press: tuple[int, int, QPoint] | None = None  # a tag pressed
        self._dragged: QTextCursor | None = None  # the tag being dragged

    # -- inserting --------------------------------------------------------- #
    def insert_tag(self, tag: str, position: int | None = None) -> None:
        """Insert tag at position (by default the cursor, or the start of
        any selection), spaced off from text it would otherwise touch, and
        focus the editor."""
        cursor = self.textCursor()
        if position is None:
            position = cursor.selectionStart()
        cursor.setPosition(position)
        document = self.document()
        before = document.characterAt(position - 1) if position > 0 else ""
        after = document.characterAt(position)
        if before and not before.isspace():
            tag = " " + tag
        # At a line or document end, after is U+2029, which still wants a
        # space so typing can carry on after the tag.
        if after not in (" ", "\t"):
            tag += " "
        cursor.insertText(tag)
        self.setTextCursor(cursor)
        self.setFocus()

    # -- finding tags under the mouse -------------------------------------- #
    def _char_at(self, point: QPoint) -> tuple[QTextBlock, int] | None:
        """The block and string index of the character under point."""
        cursor = self.cursorForPosition(point)
        position = cursor.position()
        # The caret nearest point sits on one side of the character under
        # it; left of the caret, that character comes before it.
        if point.x() < self.cursorRect(cursor).x():
            position -= 1
        if position < 0:
            return None
        block = self.document().findBlock(position)
        if (
            not block.isValid()
            or position - block.position()
            >= len(block.text().encode("utf-16-le")) // 2
        ):
            return None
        text = block.text()
        return block, string_index(text, position - block.position())

    def tag_range_at(self, point: QPoint) -> tuple[int, int] | None:
        """Document positions (start, end) of the tag under point."""
        found = self._char_at(point)
        if found is None:
            return None
        block, index = found
        text = block.text()
        tag = tag_at(text, index)
        if tag is None:
            return None
        base = block.position()
        return (
            base + utf16_index(text, tag.start),
            base + utf16_index(text, tag.end),
        )

    # -- tooltips and double-clicks ---------------------------------------- #
    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            found = self._char_at(self.viewport().mapFrom(self, event.pos()))
            tip = (
                self.tag_tooltip(found[0].text(), found[1]) if found else None
            )
            if tip:
                QToolTip.showText(event.globalPos(), tip, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        tag = self.tag_range_at(event.position().toPoint())
        if tag is None or event.button() != Qt.MouseButton.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        self._press = None
        event.accept()
        self.tag_double_clicked.emit(*tag)

    # -- dragging tags ------------------------------------------------------- #
    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._press = None
        if event.button() == Qt.MouseButton.LeftButton and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            point = event.position().toPoint()
            tag = self.tag_range_at(point)
            if tag is not None:
                self._press = (*tag, point)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press is not None and (
            event.buttons() & Qt.MouseButton.LeftButton
        ):
            start, end, pressed = self._press
            moved = event.position().toPoint() - pressed
            if moved.manhattanLength() >= QApplication.startDragDistance():
                self._press = None
                self._drag_tag(start, end)
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._press = None
        super().mouseReleaseEvent(event)

    def _drag_tag(self, start: int, end: int) -> None:
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        data = QMimeData()
        tag = cursor.selectedText()
        data.setData(TAG_MIME_TYPE, QByteArray(tag.encode()))
        data.setText(tag)  # other apps get the tag as text
        self._dragged = cursor  # follows edits until the drop
        drag = QDrag(self)
        drag.setMimeData(data)
        drag.exec(
            Qt.DropAction.MoveAction | Qt.DropAction.CopyAction,
            Qt.DropAction.MoveAction,
        )
        self._dragged = None

    def canInsertFromMimeData(self, source: QMimeData) -> bool:
        return (
            source.hasFormat(CHARACTER_MIME_TYPE)
            or source.hasFormat(NEW_TAG_MIME_TYPE)
            or source.hasFormat(TAG_MIME_TYPE)
            or super().canInsertFromMimeData(source)
        )

    def insertFromMimeData(self, source: QMimeData) -> None:
        if source.hasFormat(CHARACTER_MIME_TYPE):
            name = bytes(source.data(CHARACTER_MIME_TYPE).data()).decode()
            self.character_dropped.emit(name)
        elif source.hasFormat(NEW_TAG_MIME_TYPE):
            kind = bytes(source.data(NEW_TAG_MIME_TYPE).data()).decode()
            self.new_tag_dropped.emit(kind)
        else:
            super().insertFromMimeData(source)

    def dropEvent(self, event: QDropEvent) -> None:
        if not event.mimeData().hasFormat(TAG_MIME_TYPE):
            super().dropEvent(event)
            return
        # Clear the drop caret Qt draws while dragging.
        self.dragLeaveEvent(QDragLeaveEvent())
        tag = bytes(event.mimeData().data(TAG_MIME_TYPE).data()).decode()
        target = self._drop_position(event.position().toPoint())
        source = self._dragged if event.source() is self else None
        copy = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        # Copy, whatever happens here, so the drag never deletes anything.
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        moving = source is not None and not copy
        if moving and (
            source.selectionStart() <= target <= source.selectionEnd()
        ):
            return  # dropped on itself
        edit = QTextCursor(self.document())
        edit.beginEditBlock()
        self.insert_tag(tag, target)
        if moving:
            self._remove_moved(source)
        edit.endEditBlock()

    def _drop_position(self, point: QPoint) -> int:
        """Where a dropped tag goes: the caret nearest point, moved past
        any tag it would land inside."""
        position = self.cursorForPosition(point).position()
        block = self.document().findBlock(position)
        text = block.text()
        index = string_index(text, position - block.position())
        tag = tag_at(text, index)
        if tag is not None and tag.start < index:
            position = block.position() + utf16_index(text, tag.end)
        return position

    def _remove_moved(self, source: QTextCursor) -> None:
        """Remove a moved tag from where it was, then one space, but only if
        that would otherwise leave two side by side, or one at the start or
        end of the line. Nothing else around the tag is touched."""
        source.removeSelectedText()
        document = self.document()
        position = source.position()
        before = (
            "" if source.atBlockStart() else document.characterAt(position - 1)
        )
        after = "" if source.atBlockEnd() else document.characterAt(position)
        if after == " " and before in (" ", ""):
            source.deleteChar()
        elif before == " " and after == "":
            source.deletePreviousChar()


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
