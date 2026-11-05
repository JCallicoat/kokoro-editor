"""Characters side panel: the cast picker, the character table and the
add/edit character dialog."""

from __future__ import annotations

import json
from collections.abc import Callable

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QCursor, QGuiApplication, QPainter
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
)
from PySide6.QtWidgets import (
    QApplication,
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
    MAX_RATE,
    MIN_RATE,
    Character,
    clamp_rate,
    name_error,
    voice_aliases,
)
from kokoro_editor.settings import (
    AppSettings,
    delete_cast,
    extract_voice_names,
    load_casts,
    parse_voice_string,
    save_cast,
    save_last_cast,
    voices_request,
)
from kokoro_editor.voice_mix import VoiceMixWidget


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
        rect = option.rect
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
        rect = font_metrics.boundingRect(
            option.rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap),
            display_text,
        )
        height = max(rect.height(), option.rect.height())
        return QSize(option.rect.width(), height)


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
    """Add or edit a character: a name, the voice mix it speaks with and
    its rate."""

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
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Character" if editing else "Add Character")
        self.setMinimumWidth(360)
        self._other_names = other_names

        self.name_edit = QLineEdit(character.name)
        self.name_edit.setPlaceholderText("e.g. narrator")
        form = QFormLayout()
        form.addRow("Character:", self.name_edit)

        self.rate_spin = _RateSpinBox(lambda: settings_fn().speed)
        self.rate_spin.setValue(character.rate)
        rate_form = QFormLayout()
        rate_form.addRow("Rate:", self.rate_spin)

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

    def character(self) -> Character:
        return Character(
            self.name_edit.text().strip(),
            self.voice_mix.voice_string(),
            # The spin box holds 2 decimals; round off float noise.
            round(self.rate_spin.value(), 2),
        )

    def accept(self) -> None:
        error = name_error(self.character().name, self._other_names)
        if error:
            QMessageBox.warning(self, "Invalid name", error)
            self.name_edit.setFocus()
            return
        super().accept()


class PanelWidget(QWidget):
    """Collapsible side panel listing the characters, with Add/Remove buttons,
    above which the cast they belong to is picked, created, deleted and saved.

    The character list is the source of truth; the table only displays it.
    The cast is unsaved while the list differs from what was last saved.
    """

    COLLAPSED_WIDTH = 30
    EXPANDED_WIDTH = 220

    characters_changed = Signal()
    character_renamed = Signal(str, str)  # old name, new name

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
            QStyle.StandardPixmap.SP_DialogSaveButton, "Save cast"
        )
        self.save_cast_btn.clicked.connect(self.save_cast)
        cast_row = QHBoxLayout()
        cast_row.addWidget(self.cast_box, 1)
        cast_row.addWidget(self.new_cast_btn)
        cast_row.addWidget(self.delete_cast_btn)
        cast_row.addWidget(self.save_cast_btn)
        content_layout.addLayout(cast_row)
        content_layout.addSpacing(8)

        # Characters section: the cast's characters, with Add/Remove below
        self.title_label = QLabel("Characters")
        self.title_label.setStyleSheet("font-weight: bold;")
        content_layout.addWidget(self.title_label)
        self.table = QTableWidget(0, 2)
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
        content_layout.addWidget(self.table, 1)

        # Footer buttons
        button_row = QHBoxLayout()
        self.add_btn = QPushButton("Add")
        self.add_btn.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton)
        )
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogCloseButton)
        )
        button_row.addWidget(self.add_btn)
        button_row.addWidget(self.remove_btn)
        self.add_btn.clicked.connect(self._on_add)
        self.remove_btn.clicked.connect(self._on_remove)
        # Enter key on the panel opens the editor for the selected row
        self._enter_action = QAction(self)
        self._enter_action.setShortcut(Qt.Key.Key_Return)
        self._enter_action.triggered.connect(self._on_enter)
        self.addAction(self._enter_action)
        content_layout.addLayout(button_row)

        layout.addWidget(self.content)
        self._set_collapsed(True)
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
        self._set_collapsed(not self._collapsed)

    def _set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        self.content.setVisible(not collapsed)
        self.toggle_btn.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_ArrowRight
                if collapsed
                else QStyle.StandardPixmap.SP_ArrowLeft
            )
        )
        self.setFixedWidth(
            self.COLLAPSED_WIDTH if collapsed else self.EXPANDED_WIDTH
        )

    # -- characters --------------------------------------------------------- #
    def characters(self) -> list[Character]:
        return list(self._characters)

    def voice_aliases(self) -> dict[str, str | dict]:
        return voice_aliases(self._characters)

    def selected_character(self) -> Character | None:
        row = self.table.currentRow()
        if 0 <= row < len(self._characters):
            return self._characters[row]
        return None

    def _refresh_table(self) -> None:
        self.table.setRowCount(len(self._characters))
        for row, character in enumerate(self._characters):
            for column, text in enumerate((character.name, character.voice)):
                item = QTableWidgetItem(text)
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                # Either cell shows the whole character, rate included.
                item.setToolTip(character.tooltip())
                self.table.setItem(row, column, item)
        self.table.resizeRowsToContents()

    # -- casts -------------------------------------------------------------- #
    def cast_modified(self) -> bool:
        return self._characters != self._saved_characters

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
        taken = {n.casefold() for n in load_casts()}
        name = ""
        while True:
            name, ok = QInputDialog.getText(
                self, title, "Cast name:", text=name
            )
            name = name.strip()
            if not ok:
                return None
            if not name:
                QMessageBox.warning(self, title, "Enter a name for the cast.")
            elif name.casefold() in taken:
                QMessageBox.warning(
                    self, title, f"There is already a cast named {name}."
                )
            else:
                return name

    def _on_cast_activated(self, index: int) -> None:
        name = self.cast_box.itemText(index)
        if name == self._cast_name:
            return
        if not self.maybe_save_cast() or not self.load_cast(name):
            self._refresh_casts()  # put the selection back

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
        if reply.error() != QNetworkReply.NetworkError.NoError:
            return
        try:
            self._voices = extract_voice_names(
                json.loads(reply.readAll().data())
            )
        except (ValueError, TypeError, AttributeError):
            pass

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
        answer = QMessageBox.question(
            self,
            "Remove Character",
            f"Remove {character.name}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._characters.remove(character)
            self._refresh_table()
            self.characters_changed.emit()
