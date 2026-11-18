"""Find and replace: a panel floating over the editor's top-right corner
that highlights every match and steps through them, replacing one at a
time (asking first) or all at once."""

from __future__ import annotations

import re
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QKeyEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QToolButton,
)

from kokoro_editor.colors import DEFAULT_SEARCH_COLOR, text_color

MARGIN = 6  # between the panel and the editor's edges
REFRESH_DELAY_MS = 150  # after an edit, before the highlights are redone
ERROR_COLOR = "#e0245e"


# -- matching (no Qt) -------------------------------------------------------- #
@dataclass(frozen=True)
class Match:
    """A match, at document positions (UTF-16 units, as QTextCursor
    counts them), with the re.Match it came from for replacing."""

    start: int
    end: int
    match: re.Match


def compile_pattern(find: str, regex: bool) -> re.Pattern:
    """Case-sensitive; in regex mode, (?i) makes it insensitive. Raises
    re.error for an invalid regex."""
    return re.compile(find if regex else re.escape(find))


def find_matches(text: str, find: str, regex: bool) -> list[Match]:
    """Every non-empty match of find in text. Raises re.error for an
    invalid regex."""
    if not find:
        return []
    matches = []
    index = units = 0  # a string index and its UTF-16 position
    for m in compile_pattern(find, regex).finditer(text):
        if m.end() == m.start():  # e.g. ^ or \b: nothing to show or replace
            continue
        units += _utf16_len(text, index, m.start())
        start = units
        units += _utf16_len(text, m.start(), m.end())
        index = m.end()
        matches.append(Match(start, units, m))
    return matches


def _utf16_len(text: str, start: int, end: int) -> int:
    return len(text[start:end].encode("utf-16-le")) // 2


def replacement_for(match: re.Match, replacement: str, regex: bool) -> str:
    r"""In regex mode, \1, \g<name> and escapes like \n are expanded.
    Raises re.error for a bad group reference."""
    return match.expand(replacement) if regex else replacement


# -- the panel --------------------------------------------------------------- #
class FindPanel(QFrame):
    """Find and replace for an editor. It is created as the editor's child,
    hidden, and keeps itself in the editor's top-right corner.

    Find on its own searches; with replacement text, the action button
    replaces (asking about each match), or replaces every match at once
    when All is checked. Matching is case-sensitive."""

    def __init__(self, editor: QPlainTextEdit) -> None:
        super().__init__(editor)
        self._editor = editor
        self._matches: list[Match] = []
        self._error = ""
        self._format = QTextCharFormat()
        self.set_color(DEFAULT_SEARCH_COLOR)

        # paintEvent() draws the background, hiding the text beneath, and
        # a border, which styles like Breeze leave out for a StyledPanel.
        self.setFrameShape(QFrame.Shape.NoFrame)

        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Find")
        self.find_edit.setClearButtonEnabled(True)
        self.find_edit.setMinimumWidth(200)
        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText("Replace with")
        self.replace_edit.setClearButtonEnabled(True)
        self.regex_check = QCheckBox("Regex")
        self.regex_check.setToolTip(
            "Find with a Python regular expression; the replacement can "
            "use \\1 or \\g<name>.\nMatching is case-sensitive: start "
            "with (?i) to ignore case."
        )
        self.all_check = QCheckBox("All")
        self.all_check.setToolTip("Replace every match at once")
        self.previous_btn = QToolButton()
        self.previous_btn.setArrowType(Qt.ArrowType.UpArrow)
        self.previous_btn.setToolTip("Previous match (Ctrl+Shift+G)")
        self.next_btn = QToolButton()
        self.next_btn.setArrowType(Qt.ArrowType.DownArrow)
        self.next_btn.setToolTip("Next match")
        self.action_btn = QPushButton("Search")
        self.action_btn.setToolTip("Ctrl+G")
        self.close_btn = QToolButton()
        self.close_btn.setText("✕")
        self.close_btn.setAutoRaise(True)
        self.close_btn.setToolTip("Close (Escape)")
        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: gray;")

        grid = QGridLayout(self)
        grid.setContentsMargins(6, 4, 4, 4)
        grid.setVerticalSpacing(4)
        grid.addWidget(self.find_edit, 0, 0)
        grid.addWidget(self.regex_check, 0, 1)
        grid.addWidget(self.previous_btn, 0, 2)
        grid.addWidget(self.close_btn, 0, 4, Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self.replace_edit, 1, 0)
        grid.addWidget(self.all_check, 1, 1)
        grid.addWidget(self.next_btn, 1, 2)
        grid.addWidget(self.action_btn, 1, 3, 1, 2)
        grid.addWidget(self.status_label, 2, 0, 1, 5)

        self.find_edit.textChanged.connect(self._on_query_changed)
        self.regex_check.toggled.connect(self._on_query_changed)
        self.replace_edit.textChanged.connect(self._update_action)
        self.previous_btn.clicked.connect(lambda: self.find(-1))
        self.next_btn.clicked.connect(lambda: self.find(+1))
        self.action_btn.clicked.connect(self.activate)
        self.close_btn.clicked.connect(self.close_panel)
        for edit in (self.find_edit, self.replace_edit):
            edit.installEventFilter(self)
        # Tab cycles through these, column by column, never into the text.
        self._tab_chain = [
            self.find_edit,
            self.regex_check,
            self.replace_edit,
            self.all_check,
            self.previous_btn,
            self.next_btn,
            self.action_btn,
            self.close_btn,
        ]

        # Edits are gathered up before the matches are found again.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(REFRESH_DELAY_MS)
        self._refresh_timer.timeout.connect(self._refresh)
        editor.document().contentsChanged.connect(self._on_document_changed)
        editor.selectionChanged.connect(self._show_status)
        editor.installEventFilter(self)  # Escape
        # Resized with the editor, and when a scroll bar comes or goes.
        self._viewport = editor.viewport()
        self._viewport.installEventFilter(self)

        self._update_action()
        self.hide()

    # -- public ------------------------------------------------------------- #
    def set_color(self, color: str) -> None:
        self._format = QTextCharFormat()
        self._format.setBackground(QColor(color))
        self._format.setForeground(QColor(text_color(color)))
        if not self.isHidden():
            self._highlight()

    def open_panel(self) -> None:
        """Show the panel with the focus in Find; a selection on one line
        becomes the text to find."""
        selected = self._editor.textCursor().selectedText()
        if selected and "\u2029" not in selected:  # Qt's line separator
            if self.regex_check.isChecked():
                selected = re.escape(selected)
            self.find_edit.setText(selected)
        if self.isHidden():
            self.show()
            self.raise_()
            self._place()
            self._refresh()
        self.find_edit.setFocus()
        self.find_edit.selectAll()

    def close_panel(self) -> None:
        self.hide()
        self._refresh_timer.stop()
        self._matches = []
        self._editor.setExtraSelections([])
        self._editor.setFocus()

    def activate(self) -> None:
        """The action button (and Ctrl+G): find the next match or, with
        replacement text, replace."""
        if self.isHidden() or not self.find_edit.text():
            self.open_panel()
            if not self.find_edit.text():
                return
        if not self.replace_edit.text():
            self.find(+1)
        elif self.all_check.isChecked():
            self.replace_all()
        else:
            self.replace_next()

    def find(self, direction: int) -> Match | None:
        """Select the next (+1) or previous (-1) match from the cursor,
        wrapping around the document, and scroll to it."""
        if self.isHidden():
            self.open_panel()
        self._refresh()
        if not self._matches:
            return None
        cursor = self._editor.textCursor()
        position = cursor.selectionStart()
        on_match = self._current() is not None
        if direction > 0:
            found = next(
                (
                    m
                    for m in self._matches
                    if m.start > position
                    or (m.start == position and not on_match)
                ),
                None,
            )
            wrapped_to = self._matches[0]
        else:
            found = next(
                (m for m in reversed(self._matches) if m.start < position),
                None,
            )
            wrapped_to = self._matches[-1]
        wrapped = found is None
        self._select(found or wrapped_to)
        if wrapped:
            where = "top" if direction > 0 else "bottom"
            self._show_status(f"wrapped to the {where}")
        return found or wrapped_to

    def replace_next(self) -> None:
        """Select the next match (or keep the selected one) and ask about
        replacing it; either way, move on to the following match."""
        self._refresh()
        match = self._current() or self.find(+1)
        if match is None:
            return
        try:
            new = replacement_for(
                match.match, self.replace_edit.text(), self._regex()
            )
        except re.error as error:
            self._show_error(f"Bad replacement: {error}")
            return
        answer = QMessageBox.question(
            self.window(),
            "Replace",
            f"Replace this match with “{new}”?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return
        if answer == QMessageBox.StandardButton.Yes:
            cursor = self._editor.textCursor()
            cursor.insertText(new)
            self._editor.setTextCursor(cursor)  # just after the replacement
            self._refresh()
        self.find(+1)

    def replace_all(self) -> None:
        """Ask, then replace every match as one undoable edit."""
        self._refresh()
        if not self._matches:
            return
        replacement, regex = self.replace_edit.text(), self._regex()
        try:
            news = [
                replacement_for(m.match, replacement, regex)
                for m in self._matches
            ]
        except re.error as error:
            self._show_error(f"Bad replacement: {error}")
            return
        count = len(self._matches)
        plural = "" if count == 1 else "es"
        answer = QMessageBox.question(
            self.window(),
            "Replace All",
            f"Replace {count} match{plural} of “{self.find_edit.text()}” "
            f"with “{replacement}”?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        cursor = QTextCursor(self._editor.document())
        cursor.beginEditBlock()
        # From the end, so earlier positions stay valid.
        for m, new in zip(reversed(self._matches), reversed(news)):
            cursor.setPosition(m.start)
            cursor.setPosition(m.end, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(new)
        cursor.endEditBlock()
        self._refresh()
        self._show_status(f"replaced {count}")

    # -- matches and highlights -------------------------------------------- #
    def _regex(self) -> bool:
        return self.regex_check.isChecked()

    def _refresh(self) -> None:
        """Find the matches again and highlight them."""
        self._refresh_timer.stop()
        self._error = ""
        try:
            self._matches = find_matches(
                self._editor.toPlainText(),
                self.find_edit.text(),
                self._regex(),
            )
        except re.error as error:
            self._matches = []
            self._error = f"Invalid regex: {error}"
        self._highlight()
        self._show_status()

    def _highlight(self) -> None:
        selections = []
        document = self._editor.document()
        for m in self._matches:
            selection = QTextEdit.ExtraSelection()
            selection.cursor = QTextCursor(document)
            selection.cursor.setPosition(m.start)
            selection.cursor.setPosition(
                m.end, QTextCursor.MoveMode.KeepAnchor
            )
            selection.format = self._format
            selections.append(selection)
        self._editor.setExtraSelections(selections)

    def _current(self) -> Match | None:
        """The match the editor's selection is exactly on."""
        cursor = self._editor.textCursor()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        return next(
            (m for m in self._matches if (m.start, m.end) == (start, end)),
            None,
        )

    def _select(self, match: Match) -> None:
        cursor = self._editor.textCursor()
        cursor.setPosition(match.start)
        cursor.setPosition(match.end, QTextCursor.MoveMode.KeepAnchor)
        self._editor.setTextCursor(cursor)
        self._editor.ensureCursorVisible()

    def _on_query_changed(self) -> None:
        if not self.isHidden():
            self._refresh()

    def _on_document_changed(self) -> None:
        if not self.isHidden():
            self._refresh_timer.start()

    # -- status ------------------------------------------------------------- #
    def _show_status(self, note: str = "") -> None:
        if self.isHidden():
            return
        if self._error:
            self._show_error(self._error)
            return
        if not self.find_edit.text():
            text = ""
        elif not self._matches:
            text = "No matches"
        else:
            current = self._current()
            count = len(self._matches)
            if current is None:
                text = f"{count} match{'' if count == 1 else 'es'}"
            else:
                text = f"{self._matches.index(current) + 1} of {count}"
        if note:
            text = f"{text} ({note})" if text else note
        self.status_label.setStyleSheet("color: gray;")
        self.status_label.setText(text)

    def _show_error(self, message: str) -> None:
        self.status_label.setStyleSheet(f"color: {ERROR_COLOR};")
        self.status_label.setText(message)

    def _update_action(self) -> None:
        self.action_btn.setText(
            "Replace" if self.replace_edit.text() else "Search"
        )

    # -- looks, placement and keys ----------------------------------------- #
    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        painter.setPen(palette.color(QPalette.ColorRole.Mid))
        painter.setBrush(palette.color(QPalette.ColorRole.Window))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 4, 4)

    def focusNextPrevChild(self, next: bool) -> bool:
        """Tab and Shift+Tab from the panel's widgets come here (Qt asks
        each parent in turn); keep the focus within the panel instead of
        passing it on to the editor, where Tab would type a tab."""
        chain = self._tab_chain
        current = QApplication.focusWidget()
        index = chain.index(current) if current in chain else -1
        target = chain[(index + (1 if next else -1)) % len(chain)]
        target.setFocus(
            Qt.FocusReason.TabFocusReason
            if next
            else Qt.FocusReason.BacktabFocusReason
        )
        return True

    def _place(self) -> None:
        """In the top-right corner of the text, left of any scroll bar."""
        viewport = self._editor.viewport().geometry()
        width = min(self.sizeHint().width(), viewport.width() - 2 * MARGIN)
        self.resize(
            max(width, self.minimumSizeHint().width()),
            self.sizeHint().height(),
        )
        self.move(
            viewport.right() - self.width() - MARGIN + 1,
            viewport.top() + MARGIN,
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._viewport:
            if event.type() == QEvent.Type.Resize and not self.isHidden():
                self._place()
        elif watched is self._editor:
            if (
                event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Escape
                and not self.isHidden()
            ):
                self.close_panel()
                return True
        elif event.type() == QEvent.Type.KeyPress:
            return self._edit_key(watched, event)
        return super().eventFilter(watched, event)

    def _edit_key(self, edit: QObject, event: QKeyEvent) -> bool:
        """Enter in Find finds the next match (Shift+Enter the previous);
        in Replace, it does what the action button does. Escape closes."""
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close_panel()
            return True
        if key not in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return False
        if edit is self.replace_edit:
            self.activate()
        elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.find(-1)
        else:
            self.find(+1)
        return True
