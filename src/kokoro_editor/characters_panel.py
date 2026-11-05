"""Characters side panel: the cast picker, the character table and the
add/edit character dialog."""

from __future__ import annotations

import json
from collections.abc import Callable

from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QMimeData,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QGuiApplication,
    QKeySequence,
    QPainter,
)
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from kokoro_editor.characters import (
    CHARACTER_MIME_TYPE,
    MAX_RATE,
    MIN_RATE,
    Character,
    cast_name_error,
    character_colors,
    clamp_rate,
    name_error,
    voice_aliases,
    voice_tag,
)
from kokoro_editor.colors import suggest_color
from kokoro_editor.settings import (
    AppSettings,
    delete_cast,
    extract_voice_names,
    load_casts,
    load_last_characters,
    parse_voice_string,
    save_cast,
    save_last_cast,
    save_last_character,
    voices_request,
)
from kokoro_editor.voice_mix import VoiceMixWidget
from kokoro_editor.widgets import ColorButton

# Space between the voice column's text and its cell edges.
VOICE_PADDING = 4


def _padded(rect: QRect) -> QRect:
    return rect.adjusted(VOICE_PADDING, 0, -VOICE_PADDING, 0)


class _VoiceItemDelegate(QStyledItemDelegate):
    """Delegate that wraps text in table cells and expands row height."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        style = option.widget.style() if option.widget else None
        option.displayAlignment = (
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        # Draw the background
        if style:
            style.drawControl(
                QStyle.ControlElement.CE_ItemViewItem, option, painter
            )
        # Draw wrapped text, replacing + with newlines for display
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        display_text = text.replace("+", "\n")
        rect = _padded(option.rect)
        # Draw text with word wrap, centered vertically
        flags = (
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignVCenter
            | Qt.TextFlag.TextWordWrap
        )
        painter.drawText(rect, int(flags), display_text)

    def sizeHint(
        self,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> QSize:
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        display_text = text.replace("+", "\n")
        # Calculate wrapped height
        font_metrics = option.fontMetrics
        # Measured in the padded width it's drawn in, so wrapped lines
        # aren't clipped.
        rect = font_metrics.boundingRect(
            _padded(option.rect),
            int(Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap),
            display_text,
        )
        height = max(rect.height(), option.rect.height())
        return QSize(option.rect.width(), height)


class _CharacterTable(QTableWidget):
    """Character table whose rows can be dragged onto the editor (as the
    character's name), into other apps (as its [voice:] tag), or within
    the table to reorder them.

    A row dropped on another row is meant to go above it, and below the
    last row to the end; row_dropped(from_row, target_row) reports it,
    target_row being rowCount() for the end, and the owner moves it.
    """

    row_dropped = Signal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, 2, parent)
        self.setDragEnabled(True)
        self.setDragDropMode(QTableWidget.DragDropMode.DragDrop)
        # Copy, so Qt never removes the dragged row itself.
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDropIndicatorShown(True)

    # Only this table's own rows can be dropped here.
    def dragEnterEvent(self, event) -> None:
        if event.source() is self:
            super().dragEnterEvent(event)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.source() is self:
            super().dragMoveEvent(event)
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        from_row = self.currentRow()
        if event.source() is not self or from_row < 0:
            event.ignore()
            return
        target = self.rowAt(event.position().toPoint().y())
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.row_dropped.emit(
            from_row, target if target >= 0 else self.rowCount()
        )

    def mimeTypes(self) -> list[str]:
        return [CHARACTER_MIME_TYPE, "text/plain"]

    def mimeData(self, items) -> QMimeData:
        data = QMimeData()
        if items:
            name_item = self.item(items[0].row(), 0)
            name = name_item.text() if name_item else ""
            data.setData(CHARACTER_MIME_TYPE, QByteArray(name.encode()))
            data.setText(voice_tag(name))
        return data


class _RateSpinBox(QDoubleSpinBox):
    """Rate spin box whose tooltip gives more detail, including the
    effective pace, while Shift is held."""

    def __init__(
        self,
        speed_fn: Callable[[], float],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._speed_fn = speed_fn
        self.setRange(MIN_RATE, MAX_RATE)
        self.setSingleStep(0.05)
        self.setDecimals(2)
        self.setSuffix("×")

    def _tooltip(self, detailed: bool) -> str:
        brief = (
            "How fast the character speaks, relative to the speed in Settings."
        )
        if not detailed:
            return f"{brief}\nHold Shift for more."
        speed = self._speed_fn()
        effective = clamp_rate(speed * self.value())
        return (
            f"{brief}\n\n"
            f"At the current speed of {speed:.2f}×, this character speaks "
            f"at about {effective:.2f}×.\n"
            "It applies at each of the character's [voice:] tags; [rate:] "
            "tags in the text scale it further."
        )

    def _show_tooltip(self) -> None:
        detailed = bool(
            QGuiApplication.queryKeyboardModifiers()
            & Qt.KeyboardModifier.ShiftModifier
        )
        QToolTip.showText(QCursor.pos(), self._tooltip(detailed), self)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            self._show_tooltip()
            return True
        return super().event(event)

    # Shift is watched application-wide while hovering, since key events
    # only reach the box when it has focus.
    def enterEvent(self, event) -> None:
        QApplication.instance().installEventFilter(self)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        QApplication.instance().removeEventFilter(self)
        super().leaveEvent(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
            and event.key() == Qt.Key.Key_Shift
            and QToolTip.isVisible()
        ):
            self._show_tooltip()
        return False


class CharacterDialog(QDialog):
    """Add or edit a character: a name, the voice mix it speaks with, its
    rate, and the colour of its tags.

    A new character gets a colour unlike other_colors. One still on its
    default colour (default_color) keeps it as a default unless changed.
    """

    def __init__(
        self,
        voices: list[str],
        settings_fn: Callable[[], AppSettings],
        nam: QNetworkAccessManager,
        on_preview_active: Callable[[bool], None],
        character: Character,
        other_names: list[str],
        editing: bool = False,
        parent: QWidget | None = None,
        other_colors: list[str] | None = None,
        default_color: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Character" if editing else "Add Character")
        self.setMinimumWidth(360)
        self._other_names = other_names
        self._other_colors = other_colors or []
        # A colour of its own is kept; so is a default, unless changed.
        if character.color:
            color, self._own_color = character.color, True
        elif editing and default_color:
            color, self._own_color = default_color, False
        else:
            color, self._own_color = suggest_color(self._other_colors), True

        self.name_edit = QLineEdit(character.name)
        self.name_edit.setPlaceholderText("e.g. narrator")
        form = QFormLayout()
        form.addRow("Character:", self.name_edit)

        self.rate_spin = _RateSpinBox(lambda: settings_fn().speed)
        self.rate_spin.setValue(character.rate)
        self.color_button = ColorButton(color)
        self.color_button.setToolTip("The colour of its tags in the text")
        self.color_button.color_changed.connect(self._on_color_changed)
        another = QPushButton("↻")
        another.setToolTip("Suggest another colour unlike the others")
        another.setFixedWidth(32)
        another.clicked.connect(
            lambda: self.color_button.set_color(
                suggest_color([*self._other_colors, self.color_button.color()])
            )
        )
        color_row = QHBoxLayout()
        color_row.addWidget(self.color_button)
        color_row.addWidget(another)
        color_row.addStretch(1)
        rate_form = QFormLayout()
        rate_form.addRow("Rate:", self.rate_spin)
        rate_form.addRow("Colour:", color_row)

        # Voices are optional so characters can be jotted down quickly and
        # given voices later. The mix preview plays at the character's rate.
        self.voice_mix = VoiceMixWidget(
            settings_fn,
            nam,
            on_preview_active,
            require_first=False,
            mix_rate_fn=self.rate_spin.value,
        )
        self.voice_mix.set_choices(voices)
        self.voice_mix.set_mix(*parse_voice_string(character.voice))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.voice_mix)
        layout.addLayout(rate_form)
        layout.addWidget(buttons)

    def _on_color_changed(self, _color: str) -> None:
        self._own_color = True

    def character(self) -> Character:
        return Character(
            self.name_edit.text().strip(),
            self.voice_mix.voice_string(),
            # The spin box holds 2 decimals; round off float noise.
            round(self.rate_spin.value(), 2),
            self.color_button.color() if self._own_color else "",
        )

    def accept(self) -> None:
        error = name_error(self.character().name, self._other_names)
        if error:
            QMessageBox.warning(self, "Invalid name", error)
            self.name_edit.setFocus()
            return
        super().accept()


class CharacterPickerDialog(QDialog):
    """Pick a character from any cast, by default to insert its tag.

    It opens on the current cast, showing its unsaved edits, and in each
    cast selects the character last picked there. Only the current cast's
    characters are sent to the server, so a pick from another cast needs
    the caller to switch to that cast or import the character.

    accept_check(choice), if given, can refuse a pick to keep the dialog
    open; message, ok_text, cancel_text and option_text (a checkbox, read
    from option_check) adapt it to other uses.
    """

    def __init__(
        self,
        current_cast: str | None,
        current_characters: list[Character],
        parent: QWidget | None = None,
        title: str = "Insert Character Tag",
        message: str = "",
        ok_text: str = "",
        cancel_text: str = "",
        accept_check: Callable[[tuple[str | None, str]], bool] | None = None,
        option_text: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(260)
        self._accept_check = accept_check
        # Keyed by cast name; "" is a current cast that hasn't been saved.
        self._casts = load_casts()
        current_key = current_cast if current_cast is not None else ""
        self._casts[current_key] = list(current_characters)
        self._last_picks = load_last_characters()

        self.cast_box = QComboBox()
        if current_cast is None:
            self.cast_box.addItem("(unsaved cast)", "")
        for name in sorted(load_casts(), key=str.casefold):
            self.cast_box.addItem(name, name)
        self.character_list = QListWidget()
        self.character_list.itemDoubleClicked.connect(self.accept)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        if ok_text:
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
                ok_text
            )
        if cancel_text:
            self.buttons.button(
                QDialogButtonBox.StandardButton.Cancel
            ).setText(cancel_text)

        form = QFormLayout()
        form.addRow("Cast:", self.cast_box)
        layout = QVBoxLayout(self)
        if message:
            label = QLabel(message)
            label.setWordWrap(True)
            layout.addWidget(label)
        layout.addLayout(form)
        layout.addWidget(self.character_list, 1)
        # An optional choice for the caller, shown under the list.
        self.option_check = QCheckBox(option_text)
        self.option_check.setVisible(bool(option_text))
        layout.addWidget(self.option_check)
        layout.addWidget(self.buttons)

        self.cast_box.currentIndexChanged.connect(self._fill)
        self.cast_box.setCurrentIndex(
            max(self.cast_box.findData(current_key), 0)
        )
        self._fill()
        # Typing jumps to a name and Enter picks it.
        self.character_list.setFocus()

    def _cast_key(self) -> str:
        return self.cast_box.currentData() or ""

    def _fill(self) -> None:
        key = self._cast_key()
        characters = self._casts.get(key, [])
        self.character_list.clear()
        for character in characters:
            item = QListWidgetItem(character.name)
            item.setToolTip(character.tooltip())
            self.character_list.addItem(item)
        names = [c.name for c in characters]
        last = self._last_picks.get(key)
        if names:
            self.character_list.setCurrentRow(
                names.index(last) if last in names else 0
            )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            bool(names)
        )

    def choice(self) -> tuple[str | None, str] | None:
        """(cast name, character name) picked, the cast being None for an
        unsaved current cast; None if nothing is selected."""
        item = self.character_list.currentItem()
        if item is None:
            return None
        return self._cast_key() or None, item.text()

    def accept(self) -> None:
        choice = self.choice()
        if choice is None:
            return
        if self._accept_check is not None and not self._accept_check(choice):
            return
        save_last_character(choice[0] or "", choice[1])
        super().accept()


class PanelWidget(QWidget):
    """Collapsible side panel listing the characters, with Add/Remove buttons,
    above which the cast they belong to is picked, created, deleted and saved.

    The character list is the source of truth; the table only displays it.
    The cast is unsaved while the list differs from what was last saved.
    """

    COLLAPSED_WIDTH = 30
    EXPANDED_WIDTH = 260

    characters_changed = Signal()
    character_renamed = Signal(str, str)  # old name, new name
    collapsed_changed = Signal(bool)
    # A voice list fetch finished, whether or not it got the list.
    voices_fetched = Signal()

    def __init__(
        self,
        settings: AppSettings,
        nam: QNetworkAccessManager,
        on_preview_active: Callable[[bool], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFixedWidth(self.EXPANDED_WIDTH)
        self._settings = settings
        self._nam = nam
        self._on_preview_active = on_preview_active

        self._collapsed = True
        self._characters: list[Character] = []
        self._cast_name: str | None = None  # None: not saved as a cast yet
        self._saved_characters: list[Character] = []
        self._voices: list[str] = []
        # How often a character is tagged in the document, to warn before
        # removing one still in use; set by the main window.
        self._tag_counter: Callable[[str], int] = lambda _name: 0
        self._reply: QNetworkReply | None = None

        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Toggle button (top-right, always visible)
        style = self.style()
        self.toggle_btn = QPushButton()
        self.toggle_btn.setFlat(True)
        self.toggle_btn.setFixedWidth(24)
        self.toggle_btn.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_ArrowRight)
        )
        self.toggle_btn.clicked.connect(self._toggle)
        layout.addWidget(
            self.toggle_btn,
            0,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
        )

        # Collapsible content
        self.content = QWidget()
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(9, 6, 9, 6)

        # Cast section: pick a saved cast, or create, delete or save one
        self.cast_label = QLabel("Cast")
        self.cast_label.setStyleSheet("font-weight: bold;")
        content_layout.addWidget(self.cast_label)
        self.cast_box = QComboBox()
        self.cast_box.setToolTip("Cast")
        # Let long cast names shrink to fit the narrow panel.
        self.cast_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.cast_box.setMinimumContentsLength(1)
        self.cast_box.activated.connect(self._on_cast_activated)
        self.new_cast_btn = self._icon_button(
            QStyle.StandardPixmap.SP_FileDialogNewFolder, "New cast…"
        )
        self.new_cast_btn.clicked.connect(self._on_new_cast)
        self.delete_cast_btn = self._icon_button(
            QStyle.StandardPixmap.SP_TrashIcon, "Delete cast…"
        )
        self.delete_cast_btn.clicked.connect(self._on_delete_cast)
        self.save_cast_btn = self._icon_button(
            QStyle.StandardPixmap.SP_DialogSaveButton, "Save cast (Alt+S)"
        )
        self.save_cast_btn.clicked.connect(self.save_cast)
        cast_row = QHBoxLayout()
        cast_row.addWidget(self.cast_box, 1)
        cast_row.addWidget(self.new_cast_btn)
        cast_row.addWidget(self.delete_cast_btn)
        cast_row.addWidget(self.save_cast_btn)
        content_layout.addLayout(cast_row)
        content_layout.addSpacing(8)

        # Characters section: Add/Remove and move buttons above the table
        self.title_label = QLabel("Characters")
        self.title_label.setStyleSheet("font-weight: bold;")
        content_layout.addWidget(self.title_label)
        self.table = _CharacterTable()
        self.table.setHorizontalHeaderLabels(["Character", "Voice"])
        # Name column fits its widest cell; the voice column takes the rest.
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setVisible(False)
        self.table.verticalHeader().setVisible(False)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        # Voice column: wrap and ellipsize text
        self.table.setItemDelegateForColumn(1, _VoiceItemDelegate(self.table))

        button_row = QHBoxLayout()
        self.add_btn = QPushButton("Add")
        self.add_btn.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton)
        )
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogCloseButton)
        )
        self.up_btn = self._icon_button(
            QStyle.StandardPixmap.SP_ArrowUp, "Move character up"
        )
        self.down_btn = self._icon_button(
            QStyle.StandardPixmap.SP_ArrowDown, "Move character down"
        )
        button_row.addWidget(self.add_btn)
        button_row.addWidget(self.remove_btn)
        button_row.addWidget(self.up_btn)
        button_row.addWidget(self.down_btn)
        self.add_btn.clicked.connect(self._on_add)
        self.remove_btn.clicked.connect(self._on_remove)
        self.up_btn.clicked.connect(lambda: self._move_selected(-1))
        self.down_btn.clicked.connect(lambda: self._move_selected(+1))
        self.table.row_dropped.connect(self._on_row_dropped)
        self.table.itemSelectionChanged.connect(self._update_move_buttons)
        # Enter key on the panel opens the editor for the selected row
        self._enter_action = QAction(self)
        self._enter_action.setShortcut(Qt.Key.Key_Return)
        self._enter_action.triggered.connect(self._on_enter)
        self.addAction(self._enter_action)
        # Delete removes the selected character, only while the table has
        # focus, so Delete keeps its meaning everywhere else.
        self._delete_action = QAction(self.table)
        self._delete_action.setShortcut(QKeySequence.StandardKey.Delete)
        self._delete_action.setShortcutContext(
            Qt.ShortcutContext.WidgetShortcut
        )
        self._delete_action.triggered.connect(self._on_remove)
        self.table.addAction(self._delete_action)
        content_layout.addLayout(button_row)
        content_layout.addWidget(self.table, 1)

        layout.addWidget(self.content)
        self.set_collapsed(True)
        self._update_move_buttons()
        self.characters_changed.connect(self._update_cast_buttons)
        self._refresh_casts()
        # Fetch voices on startup so they're cached for the Add dialog
        self._fetch_voices()

    def _icon_button(
        self, pixmap: QStyle.StandardPixmap, tooltip: str
    ) -> QPushButton:
        button = QPushButton()
        button.setIcon(self.style().standardIcon(pixmap))
        button.setToolTip(tooltip)
        return button

    def _toggle(self) -> None:
        self.set_collapsed(not self._collapsed)

    def is_collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        changed = collapsed != self._collapsed
        self._collapsed = collapsed
        self.content.setVisible(not collapsed)
        self.toggle_btn.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_ArrowRight
                if collapsed
                else QStyle.StandardPixmap.SP_ArrowLeft
            )
        )
        self.toggle_btn.setToolTip(
            f"{'Show' if collapsed else 'Hide'} the characters panel (Alt+-)"
        )
        self.setFixedWidth(
            self.COLLAPSED_WIDTH if collapsed else self.EXPANDED_WIDTH
        )
        if changed:
            self.collapsed_changed.emit(collapsed)

    # -- characters --------------------------------------------------------- #
    def characters(self) -> list[Character]:
        return list(self._characters)

    def colors(self) -> dict[str, str]:
        """Each character's tag colour, by casefolded name."""
        return character_colors(self._characters)

    def voice_aliases(self) -> dict[str, str | dict]:
        return voice_aliases(self._characters)

    def selected_character(self) -> Character | None:
        row = self.table.currentRow()
        if 0 <= row < len(self._characters):
            return self._characters[row]
        return None

    def _refresh_table(self) -> None:
        self.table.setRowCount(len(self._characters))
        colors = self.colors()
        for row, character in enumerate(self._characters):
            for column, text in enumerate((character.name, character.voice)):
                item = QTableWidgetItem(text)
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                # Either cell shows the whole character, rate included.
                item.setToolTip(character.tooltip())
                if column == 0:  # a swatch of its tag colour
                    item.setData(
                        Qt.ItemDataRole.DecorationRole,
                        QColor(colors[character.name.casefold()]),
                    )
                self.table.setItem(row, column, item)
        self.table.resizeRowsToContents()
        self._update_move_buttons()

    # -- reordering --------------------------------------------------------- #
    def _move_character(self, from_row: int, to_row: int) -> None:
        """Move the character at from_row so it ends up at to_row."""
        count = len(self._characters)
        if not (0 <= from_row < count and 0 <= to_row < count):
            return
        if from_row != to_row:
            self._characters.insert(to_row, self._characters.pop(from_row))
            self._refresh_table()
            self.characters_changed.emit()
        self.table.selectRow(to_row)

    def _move_selected(self, step: int) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self._move_character(row, row + step)

    def _on_row_dropped(self, from_row: int, target_row: int) -> None:
        # Above the target row, which shifts up once from_row is taken out.
        self._move_character(
            from_row, target_row - 1 if target_row > from_row else target_row
        )

    def _update_move_buttons(self) -> None:
        row = self.table.currentRow()
        selected = self.table.selectionModel().hasSelection() and row >= 0
        self.up_btn.setEnabled(selected and row > 0)
        self.down_btn.setEnabled(selected and row < len(self._characters) - 1)

    def set_tag_counter(self, counter: Callable[[str], int]) -> None:
        self._tag_counter = counter

    def voices(self) -> list[str] | None:
        """The server's voices, or None if they haven't loaded."""
        return list(self._voices) or None

    def voices_pending(self) -> bool:
        """Whether the voice list is being fetched."""
        return self._reply is not None

    def set_characters(self, characters: list[Character]) -> None:
        """Replace the current cast's characters, leaving it unsaved."""
        self._characters = list(characters)
        self._refresh_table()
        self.characters_changed.emit()

    def character_named(self, name: str) -> Character | None:
        for character in self._characters:
            if character.name == name:
                return character
        return None

    # -- casts -------------------------------------------------------------- #
    def cast_name(self) -> str | None:
        """The current cast's name, or None if it hasn't been saved yet."""
        return self._cast_name

    def cast_modified(self) -> bool:
        return self._characters != self._saved_characters

    def switch_cast(self, name: str) -> bool:
        """Make the saved cast called name current, offering to save
        changes first. False if the user cancelled or there is no such
        cast."""
        if name == self._cast_name:
            return True
        if not self.maybe_save_cast() or not self.load_cast(name):
            self._refresh_casts()  # put the dropdown's selection back
            return False
        return True

    def load_cast(self, name: str) -> bool:
        """Show the saved cast called name. False if there is none."""
        characters = load_casts().get(name)
        if characters is None:
            return False
        self._set_cast(name, characters)
        return True

    def _set_cast(self, name: str | None, characters: list[Character]) -> None:
        """Make name the current cast, with characters as its saved state."""
        self._cast_name = name
        self._characters = list(characters)
        self._saved_characters = list(characters)
        save_last_cast(name or "")
        self._refresh_table()
        self._refresh_casts()
        self.characters_changed.emit()

    def _refresh_casts(self) -> None:
        names = sorted(load_casts(), key=str.casefold)
        self.cast_box.blockSignals(True)
        self.cast_box.clear()
        self.cast_box.addItems(names)
        self.cast_box.setCurrentIndex(
            names.index(self._cast_name) if self._cast_name in names else -1
        )
        self.cast_box.blockSignals(False)
        self._update_cast_buttons()

    def _update_cast_buttons(self) -> None:
        self.delete_cast_btn.setEnabled(self._cast_name is not None)
        self.save_cast_btn.setEnabled(self.cast_modified())

    def maybe_save_cast(self) -> bool:
        """Offer to save unsaved cast changes. False if the user cancelled."""
        if not self.cast_modified():
            return True
        question = (
            f"Save changes to the cast {self._cast_name}?"
            if self._cast_name is not None
            else "Save the current characters as a cast?"
        )
        Btn = QMessageBox.StandardButton
        answer = QMessageBox.warning(
            self,
            "Unsaved cast",
            question,
            Btn.Save | Btn.Discard | Btn.Cancel,
            Btn.Save,
        )
        if answer == Btn.Save:
            return self.save_cast()
        return answer == Btn.Discard

    def save_cast(self) -> bool:
        """Save the current cast, asking for a name if it has none yet.
        False if the user cancelled."""
        name = self._cast_name
        if name is None:
            name = self._ask_cast_name("Save Cast")
            if name is None:
                return False
        save_cast(name, self._characters)
        self._set_cast(name, self._characters)
        return True

    def _ask_cast_name(self, title: str) -> str | None:
        """A name not used by any saved cast, or None if cancelled."""
        taken = list(load_casts())
        name = ""
        while True:
            name, ok = QInputDialog.getText(
                self, title, "Cast name:", text=name
            )
            name = name.strip()
            if not ok:
                return None
            error = cast_name_error(name, taken)
            if not error:
                return name
            QMessageBox.warning(self, title, error)

    # -- changes made from the Casts settings tab --------------------------- #
    def cast_renamed(self, old: str, new: str) -> None:
        if self._cast_name == old:
            self._cast_name = new
            save_last_cast(new)
        self._refresh_casts()

    def casts_changed(self, name: str) -> None:
        """The saved cast called name changed; show it if it's current."""
        if name == self._cast_name:
            self.load_cast(name)
        else:
            self._refresh_casts()

    def _on_cast_activated(self, index: int) -> None:
        # On cancel switch_cast puts the dropdown back.
        self.switch_cast(self.cast_box.itemText(index))

    def _on_new_cast(self) -> None:
        if not self.maybe_save_cast():
            return
        name = self._ask_cast_name("New Cast")
        if name is None:
            return
        save_cast(name, [])
        self._set_cast(name, [])

    def _on_delete_cast(self) -> None:
        name = self._cast_name
        if name is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete Cast",
            f"Delete the cast {name} and its characters?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            delete_cast(name)
            self._set_cast(None, [])

    def set_settings(self, settings: AppSettings) -> None:
        # Refetch for a new server, or retry if the last attempt failed.
        retry = not self._voices and self._reply is None
        refetch = settings.base_url != self._settings.base_url
        self._settings = settings
        if refetch or retry:
            self._fetch_voices()

    def _fetch_voices(self) -> None:
        self._cancel_fetch()
        reply = self._nam.get(voices_request(self._settings.base_url))
        self._reply = reply
        reply.finished.connect(lambda r=reply: self._on_voices(r))

    def _cancel_fetch(self) -> None:
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.abort()

    def _on_voices(self, reply: QNetworkReply) -> None:
        reply.deleteLater()
        if reply is not self._reply:  # cancelled or superseded
            return
        self._reply = None
        if reply.error() == QNetworkReply.NetworkError.NoError:
            try:
                self._voices = extract_voice_names(
                    json.loads(reply.readAll().data())
                )
            except (ValueError, TypeError, AttributeError):
                pass
        self.voices_fetched.emit()

    def _on_cell_double_clicked(self, row: int, _column: int) -> None:
        self._edit_row(row)

    def _on_enter(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        self._edit_row(row)

    def _edit_row(self, row: int) -> None:
        if 0 <= row < len(self._characters):
            self._open_character_dialog(row)

    def _on_add(self) -> None:
        self._open_character_dialog()

    def _open_character_dialog(self, editing_row: int = -1) -> None:
        editing = editing_row >= 0
        colors = self.colors()
        current = self._characters[editing_row] if editing else None
        dialog = CharacterDialog(
            self._voices,
            lambda: self._settings,
            self._nam,
            self._on_preview_active,
            self._characters[editing_row] if editing else Character(""),
            [
                c.name
                for i, c in enumerate(self._characters)
                if i != editing_row
            ],
            editing=editing,
            parent=self,
            other_colors=[
                colors[c.name.casefold()]
                for c in self._characters
                if c is not current
            ],
            default_color=colors[current.name.casefold()] if current else "",
        )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        character = dialog.character()
        dialog.deleteLater()
        if not accepted:
            return
        old_name = None
        if editing:
            row = editing_row
            old_name = self._characters[row].name
            self._characters[row] = character
        else:
            row = len(self._characters)
            self._characters.append(character)
        self._refresh_table()
        self.table.selectRow(row)
        self.characters_changed.emit()
        if old_name is not None and old_name != character.name:
            self.character_renamed.emit(old_name, character.name)

    def _on_remove(self) -> None:
        character = self.selected_character()
        if character is None:
            return
        question = f"Remove {character.name}?"
        uses = self._tag_counter(character.name)
        if uses:
            times = "once" if uses == 1 else f"{uses} times"
            question = (
                f"{character.name} is tagged {times} in the document. "
                "Those tags won't speak until they're replaced, which Play "
                "and Render will offer to do.\n\n" + question
            )
        answer = QMessageBox.question(
            self,
            "Remove Character",
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._characters.remove(character)
            self._refresh_table()
            self.characters_changed.emit()
