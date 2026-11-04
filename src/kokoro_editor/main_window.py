"""Main editor window: text editing, file handling and speech playback."""

from __future__ import annotations

import json
import os
from collections.abc import Callable

from PySide6.QtCore import (
    QByteArray,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    QSize,
    QTimer,
)
from PySide6.QtGui import QAction, QCursor, QKeySequence, QPainter
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from kokoro_editor import audio
from kokoro_editor.audio import PCMPlayer
from kokoro_editor.render_dialog import RenderDialog
from kokoro_editor.settings import (
    REQUEST_INACTIVITY_TIMEOUT_MS,
    AppSettings,
    add_recent_file,
    describe_http_error,
    extract_voice_names,
    fmt_time,
    load_last_dir,
    load_recent_files,
    load_window_geometry,
    parse_voice_string,
    remove_recent_file,
    save_last_dir,
    save_recent_files,
    save_window_geometry,
    speech_payload,
    speech_request,
    voices_request,
)
from kokoro_editor.settings_dialog import SettingsDialog
from kokoro_editor.voice_mix import VoiceMixWidget
from kokoro_editor.widgets import SeekSlider

APP_NAME = "Kokoro Editor"
UNTITLED = "Untitled"
FILE_FILTER = "Text files (*.txt);;All files (*)"


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

    def helpEvent(self, event, _widget, option, index) -> bool:
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        if text:
            QToolTip.showText(QCursor.pos(), text, option.widget)
        return True


class CharacterDialog(QDialog):
    """Add or edit a character: a name and the voice mix it speaks with."""

    def __init__(
        self,
        voices: list[str],
        settings_fn: Callable[[], AppSettings],
        nam: QNetworkAccessManager,
        on_preview_active: Callable[[bool], None],
        name: str = "",
        voice_string: str = "",
        editing: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Character" if editing else "Add Character")
        self.setMinimumWidth(360)

        self.name_edit = QLineEdit(name)
        form = QFormLayout()
        form.addRow("Character:", self.name_edit)

        # Voices are optional so characters can be jotted down quickly and
        # given voices later.
        self.voice_mix = VoiceMixWidget(
            settings_fn, nam, on_preview_active, require_first=False
        )
        self.voice_mix.set_choices(voices)
        self.voice_mix.set_mix(*parse_voice_string(voice_string))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.voice_mix)
        layout.addWidget(buttons)


class PanelWidget(QWidget):
    """Collapsible side panel with a Characters label and Add/Remove buttons."""

    COLLAPSED_WIDTH = 30
    EXPANDED_WIDTH = 220

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

        # Header label
        self.title_label = QLabel("Characters")
        self.title_label.setStyleSheet("font-weight: bold;")
        content_layout.addWidget(self.title_label)

        # Body — table of characters
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
        self.table.cellChanged.connect(self._on_cell_changed)
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
        # Fetch voices on startup so they're cached for the Add dialog
        self._fetch_voices()

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
        name_item = self.table.item(row, 0)
        voice_item = self.table.item(row, 1)
        self._open_character_dialog(
            row,
            name_item.text() if name_item else "",
            voice_item.text() if voice_item else "",
        )

    def _on_add(self) -> None:
        self._open_character_dialog()

    def _open_character_dialog(
        self,
        editing_row: int = -1,
        initial_name: str = "",
        initial_voice: str = "",
    ) -> None:
        dialog = CharacterDialog(
            self._voices,
            lambda: self._settings,
            self._nam,
            self._on_preview_active,
            initial_name,
            initial_voice,
            editing=editing_row >= 0,
            parent=self,
        )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        name = dialog.name_edit.text().strip()
        voice_str = dialog.voice_mix.voice_string()
        dialog.deleteLater()
        if not accepted or not name:
            return
        row = editing_row
        if row < 0:
            row = self.table.rowCount()
            self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(name))
        self.table.setItem(row, 1, QTableWidgetItem(voice_str))
        self.table.resizeRowsToContents()
        self.table.scrollToBottom()

    def _on_remove(self) -> None:
        current = self.table.currentRow()
        if current < 0:
            return
        answer = QMessageBox.question(
            self,
            "Remove Character",
            "Remove this character?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.table.removeRow(current)
            self.table.resizeRowsToContents()

    def _on_cell_changed(self, row: int, _column: int) -> None:
        self.table.resizeRowsToContents()
        item = self.table.item(row, 0)
        if item:
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
        item = self.table.item(row, 1)
        if item:
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        geometry = load_window_geometry()
        if geometry is None or not self.restoreGeometry(geometry):
            self.resize(720, 480)

        self.settings = AppSettings.load()
        self._path: str | None = None
        self._nam = QNetworkAccessManager(self)
        self._reply: QNetworkReply | None = None
        self._player: PCMPlayer | None = None
        self._error_body = b""
        self._warning = ""
        self._resume_after_preview = False

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)

        # Widgets
        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText(
            "Type or open some text, then press Play to hear it."
        )
        self.text_edit.document().modificationChanged.connect(
            self.setWindowModified
        )

        style = self.style()
        self._icon_play = style.standardIcon(
            QStyle.StandardPixmap.SP_MediaPlay
        )
        self._icon_pause = style.standardIcon(
            QStyle.StandardPixmap.SP_MediaPause
        )
        self.play_btn = QPushButton()
        self.play_btn.clicked.connect(self._on_play_clicked)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaStop)
        )
        self.stop_btn.clicked.connect(self._on_stop_clicked)
        self.render_btn = QPushButton("Render…")
        self.render_btn.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)
        )
        self.render_btn.setToolTip(
            "Render the selection (or the whole document) to an audio file"
        )
        self.render_btn.clicked.connect(self._on_render_clicked)
        self.ssml_check = QCheckBox("SSML")
        self.ssml_check.setToolTip("Send the text to the server as SSML")
        self.ssml_check.setChecked(self.settings.ssml)
        self.ssml_check.toggled.connect(self._on_ssml_toggled)

        self.seek_slider = SeekSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setToolTip(
            "Click or drag to seek within the buffered audio.\n"
            "Alt+Left/Right scrubs back and forward."
        )
        self.seek_slider.sliderReleased.connect(self._on_slider_released)

        self.status_label = QLabel("Ready")
        # Let long status messages be clipped instead of forcing the window wider.
        self.status_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.status_label.setMinimumWidth(1)
        self.summary_label = QLabel()
        self.summary_label.setStyleSheet("color: gray;")

        # Layout
        controls = QHBoxLayout()
        controls.addWidget(self.play_btn)
        controls.addWidget(self.stop_btn)
        controls.addWidget(self.render_btn)
        controls.addWidget(self.ssml_check)
        controls.addStretch(1)
        controls.addWidget(self.summary_label)

        self.panel = PanelWidget(
            self.settings, self._nam, self._on_preview_active, self
        )

        top = QHBoxLayout()
        top.addWidget(self.text_edit, 1)
        top.addWidget(self.panel)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(top)
        layout.addLayout(controls)
        layout.addWidget(self.seek_slider)
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)

        self._create_menus()
        self._update_title()
        self._apply_settings()
        self._refresh_ui()
        self.text_edit.setFocus()

    # -- menus -------------------------------------------------------------- #
    def _action(self, text: str, slot, shortcut=None) -> QAction:
        action = QAction(text, self)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(slot)
        return action

    def _create_menus(self) -> None:
        Key = QKeySequence.StandardKey
        edit = self.text_edit
        bar = self.menuBar()

        file_menu = bar.addMenu("&File")
        file_menu.addAction(self._action("&New", self.new_document, Key.New))
        file_menu.addAction(
            self._action("&Open…", self.open_document, Key.Open)
        )
        self.recent_menu = file_menu.addMenu("Open &Recent")
        # Rebuilt on every show so lists saved by other running instances appear too.
        self.recent_menu.aboutToShow.connect(self._update_recent_menu)
        self._update_recent_menu()
        file_menu.addAction(self._action("&Save", self.save, Key.Save))
        file_menu.addAction(
            self._action("Save &As…", self.save_as, "Ctrl+Shift+S")
        )
        file_menu.addAction(
            self._action("&Close", self.close_document, "Ctrl+W")
        )
        file_menu.addSeparator()
        file_menu.addAction(
            self._action("Se&ttings…", self._open_settings, Key.Preferences)
        )
        file_menu.addSeparator()
        file_menu.addAction(self._action("&Quit", self.close, "Ctrl+Q"))

        edit_menu = bar.addMenu("&Edit")
        undo = self._action("&Undo", edit.undo, Key.Undo)
        redo = self._action("&Redo", edit.redo, Key.Redo)
        cut = self._action("Cu&t", edit.cut, Key.Cut)
        copy = self._action("&Copy", edit.copy, Key.Copy)
        paste = self._action("&Paste", edit.paste, Key.Paste)
        select_all = self._action("Select &All", edit.selectAll, Key.SelectAll)
        for action in (undo, redo, cut, copy):
            action.setEnabled(False)
        edit.undoAvailable.connect(undo.setEnabled)
        edit.redoAvailable.connect(redo.setEnabled)
        edit.copyAvailable.connect(cut.setEnabled)
        edit.copyAvailable.connect(copy.setEnabled)
        edit_menu.addActions([undo, redo])
        edit_menu.addSeparator()
        edit_menu.addActions([cut, copy, paste])
        edit_menu.addSeparator()
        edit_menu.addAction(select_all)

        playback_menu = bar.addMenu("&Playback")
        self.play_action = self._action(
            "&Play", self._on_play_clicked, "Ctrl+Space"
        )
        self.stop_action = self._action(
            "&Stop", self._on_stop_clicked, "Ctrl+."
        )
        self.back_action = self._action(
            "Scrub &Back", lambda: self._scrub(-1), "Alt+Left"
        )
        self.forward_action = self._action(
            "Scrub &Forward", lambda: self._scrub(+1), "Alt+Right"
        )
        playback_menu.addActions([self.play_action, self.stop_action])
        playback_menu.addSeparator()
        playback_menu.addActions([self.back_action, self.forward_action])

    # -- documents ---------------------------------------------------------- #
    def _update_title(self) -> None:
        name = os.path.basename(self._path) if self._path else UNTITLED
        # Qt replaces [*] with "*" while the window is marked modified.
        self.setWindowTitle(f"{APP_NAME} — {name}[*]")
        self.setWindowModified(self.text_edit.document().isModified())

    def _dialog_dir(self) -> str:
        if self._path:
            return os.path.dirname(self._path)
        last_dir = load_last_dir()
        return last_dir if os.path.isdir(last_dir) else os.getcwd()

    def _remember_file(self, path: str) -> None:
        add_recent_file(path)
        save_last_dir(os.path.dirname(path))

    def _update_recent_menu(self) -> None:
        menu = self.recent_menu
        menu.clear()
        files = load_recent_files()
        home = os.path.expanduser("~")
        for number, path in enumerate(files, start=1):
            shown = (
                "~" + path[len(home) :]
                if path.startswith(home + os.sep)
                else path
            )
            # Escape & so Qt doesn't treat it as a mnemonic marker.
            action = menu.addAction(f"&{number}  {shown.replace('&', '&&')}")
            action.triggered.connect(
                lambda _=False, p=path: self._open_recent(p)
            )
        if not files:
            menu.addAction("No recent files").setEnabled(False)
        menu.addSeparator()
        clear = menu.addAction("&Clear List")
        clear.setEnabled(bool(files))
        clear.triggered.connect(self._clear_recent)

    def _open_recent(self, path: str) -> None:
        if not os.path.isfile(path):
            remove_recent_file(path)
            QMessageBox.warning(
                self,
                "File not found",
                f"{path}\n\nno longer exists and was removed from the recent files list.",
            )
            return
        if self.maybe_save():
            self.open_path(path)

    def _clear_recent(self) -> None:
        save_recent_files([])

    def _set_document(self, text: str, path: str | None) -> None:
        # Opening, closing or starting a new document ends playback of the old one.
        if self._player is not None:
            self._end_session("Stopped")
        self.text_edit.setPlainText(text)  # also clears the undo history
        self.text_edit.document().setModified(False)
        self._path = path
        self._update_title()

    def maybe_save(self) -> bool:
        """Offer to save unsaved changes. Returns False if the user cancelled."""
        if not self.text_edit.document().isModified():
            return True
        name = os.path.basename(self._path) if self._path else UNTITLED
        Btn = QMessageBox.StandardButton
        answer = QMessageBox.warning(
            self,
            "Unsaved changes",
            f"Save changes to {name} before closing?",
            Btn.Save | Btn.Discard | Btn.Cancel,
            Btn.Save,
        )
        if answer == Btn.Save:
            return self.save()
        return answer == Btn.Discard

    def new_document(self) -> None:
        if self.maybe_save():
            self._set_document("", None)

    def close_document(self) -> None:
        self.new_document()

    def open_document(self) -> None:
        if not self.maybe_save():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open", self._dialog_dir(), FILE_FILTER
        )
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> None:
        """Load a file. A path that does not exist yet starts a new document
        that will be saved there."""
        path = os.path.abspath(path)
        if not os.path.exists(path):
            self._set_document("", path)
            return
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as exc:
            QMessageBox.critical(
                self, "Could not open file", f"{path}\n\n{exc}"
            )
            return
        self._set_document(text, path)
        self._remember_file(path)
        self.status_label.setText(f"Opened {path}")

    def save(self) -> bool:
        if self._path is None:
            return self.save_as()
        return self._write(self._path)

    def save_as(self) -> bool:
        start = self._path or os.path.join(
            self._dialog_dir(), f"{UNTITLED}.txt"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Save As", start, FILE_FILTER
        )
        if not path:
            return False
        return self._write(path)

    def _write(self, path: str) -> bool:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.text_edit.toPlainText())
        except OSError as exc:
            QMessageBox.critical(
                self, "Could not save file", f"{path}\n\n{exc}"
            )
            return False
        self._path = os.path.abspath(path)
        self.text_edit.document().setModified(False)
        self._update_title()
        self._remember_file(self._path)
        self.status_label.setText(f"Saved {path}")
        return True

    # -- UI helpers --------------------------------------------------------- #
    def _apply_settings(self) -> None:
        """Push the current settings into the UI (at startup and after Settings)."""
        self.text_edit.setFont(self.settings.editor_font())
        self.panel.set_settings(self.settings)
        self._update_summary()

    def _update_summary(self) -> None:
        s = self.settings
        self.summary_label.setText(
            f"Voice: {s.voice_string or '(not set)'}  ·  speed {s.speed:g}  ·  volume ×{s.volume_multiplier:g}"
        )

    def _refresh_ui(self) -> None:
        active = self._player is not None
        if not active:
            text, icon = "Play", self._icon_play
        elif self._player.paused:
            text, icon = "Resume", self._icon_play
        else:
            text, icon = "Pause", self._icon_pause
        self.play_btn.setText(text)
        self.play_btn.setIcon(icon)
        self.play_action.setText(f"&{text}")
        for widget in (
            self.stop_btn,
            self.seek_slider,
            self.stop_action,
            self.back_action,
            self.forward_action,
        ):
            widget.setEnabled(active)
        if not active and not self.seek_slider.isSliderDown():
            self.seek_slider.blockSignals(True)
            self.seek_slider.setRange(0, 0)
            self.seek_slider.blockSignals(False)

    def _scrub(self, direction: int) -> None:
        if self._player is not None:
            self._player.seek_by(direction * self.settings.scrub_step_s)
            self._tick()

    def _on_slider_released(self) -> None:
        if self._player is not None:
            self._player.seek(self.seek_slider.sliderPosition() / 1000)
            self._tick()

    def _on_ssml_toggled(self, checked: bool) -> None:
        self.settings.ssml = checked
        self.settings.save()

    def _open_settings(self) -> None:
        dialog = SettingsDialog(
            self.settings, self._nam, self._on_preview_active, self
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings = dialog.result_settings()
            self.settings.save()
            self._apply_settings()
        dialog.deleteLater()

    def _on_preview_active(self, active: bool) -> None:
        """Pause document playback while a voice preview plays, then resume
        it, unless it was already paused."""
        if active:
            player = self._player
            self._resume_after_preview = (
                player is not None and not player.paused
            )
            if self._resume_after_preview:
                player.set_paused(True)
        elif self._resume_after_preview:
            self._resume_after_preview = False
            if self._player is not None:
                self._player.set_paused(False)
        self._refresh_ui()

    def _text_to_speak(self) -> str:
        """The selection if there is one, otherwise the whole document."""
        cursor = self.text_edit.textCursor()
        if cursor.hasSelection():
            # selectedText() separates paragraphs with U+2029.
            return cursor.selectedText().replace("\u2029", "\n").strip()
        return self.text_edit.toPlainText().strip()

    def _ready_to_speak(self, text: str) -> bool:
        if not text:
            self.status_label.setText("Nothing to speak.")
            return False
        if not self.settings.voice_string:
            self.status_label.setText("Choose a voice first.")
            self._open_settings()
        return bool(self.settings.voice_string)

    def _on_render_clicked(self) -> None:
        text = self._text_to_speak()
        if not self._ready_to_speak(text):
            return
        stem = UNTITLED
        if self._path:
            stem = os.path.splitext(os.path.basename(self._path))[0]
        directory = self._dialog_dir()
        selection = self.text_edit.textCursor().hasSelection()
        dialog = RenderDialog(
            self.settings,
            text,
            os.path.join(directory, stem),
            selection,
            self._nam,
            self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.status_label.setText(f"Rendered {dialog.saved_path}")

    # -- session control ---------------------------------------------------- #
    def _on_play_clicked(self) -> None:
        if self._player is None:
            self._start()
        else:
            self._player.set_paused(not self._player.paused)
            self._refresh_ui()
            self._tick()

    def _on_stop_clicked(self) -> None:
        if self._player is not None:
            self._end_session("Stopped")

    def _start(self) -> None:
        if audio.sd is None:
            QMessageBox.critical(
                self,
                "Audio output unavailable",
                "The 'sounddevice' package or the PortAudio library could not be loaded:\n\n"
                f"{audio.SD_ERROR}",
            )
            return
        text = self._text_to_speak()
        if not self._ready_to_speak(text):
            return

        s = self.settings
        player = PCMPlayer(s.sample_rate, s.prebuffer_ms)
        try:
            player.open()
        except Exception as exc:
            QMessageBox.critical(self, "Could not open audio device", str(exc))
            return

        payload = speech_payload(s, text, "pcm", stream=True)
        request = speech_request(s.base_url, REQUEST_INACTIVITY_TIMEOUT_MS)
        reply = self._nam.post(
            request, QByteArray(json.dumps(payload).encode("utf-8"))
        )
        reply.readyRead.connect(lambda r=reply: self._on_ready(r))
        reply.finished.connect(lambda r=reply: self._on_finished(r))

        self._player = player
        self._reply = reply
        self._error_body = b""
        self._warning = ""
        self._timer.start()
        self._refresh_ui()
        self._tick()

    def _end_session(self, message: str, drain: bool = False) -> None:
        self._timer.stop()
        reply, self._reply = (
            self._reply,
            None,
        )  # detach first so late signals are ignored
        if reply is not None:
            reply.abort()
        player, self._player = self._player, None
        if player is not None:
            player.close(drain)
        self.status_label.setText(message)
        self._refresh_ui()

    # -- network callbacks -------------------------------------------------- #
    def _on_ready(self, reply: QNetworkReply) -> None:
        if reply is not self._reply or self._player is None:
            return
        data = bytes(reply.readAll().data())
        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        if status is not None and status >= 400:
            self._error_body += data  # an error body is JSON/text, not audio
        else:
            self._player.feed(data)

    def _on_finished(self, reply: QNetworkReply) -> None:
        if reply is not self._reply:  # aborted by us, or superseded
            reply.deleteLater()
            return
        self._reply = None
        player = self._player
        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        error = reply.error()
        leftover = bytes(reply.readAll().data())
        error_text = reply.errorString()
        reply.deleteLater()
        if player is None:
            return

        if status is not None and status >= 400:
            self._error_body += leftover
            body = self._error_body
            self._end_session("Request failed.")
            QMessageBox.warning(
                self, "Server error", describe_http_error(status, body)
            )
            return

        if leftover:
            player.feed(leftover)

        if error != QNetworkReply.NetworkError.NoError:
            if player.snapshot().total_bytes == 0:
                self._end_session("Request failed.")
                QMessageBox.warning(self, "Request failed", error_text)
                return
            self._warning = "  ⚠ connection lost; playing what was received"

        player.mark_complete()

    # -- periodic status ---------------------------------------------------- #
    def _tick(self) -> None:
        player = self._player
        if player is None:
            return
        st = player.snapshot()
        if st.finished:
            if st.total_bytes == 0:
                self._end_session("The server returned no audio.")
            else:
                self._end_session("Finished.", drain=True)
            return

        if st.paused:
            state = "Paused"
        elif st.total_bytes == 0 and not st.complete:
            state = "Waiting for server…"
        elif not st.primed:
            state = "Buffering…"
        else:
            state = "Playing"
        position_s = st.position_s
        if self.seek_slider.isSliderDown():
            position_s = (
                self.seek_slider.sliderPosition() / 1000
            )  # show where the handle is
        else:
            self.seek_slider.blockSignals(True)
            self.seek_slider.setRange(0, int(st.total_s * 1000))
            self.seek_slider.setValue(int(st.position_s * 1000))
            self.seek_slider.blockSignals(False)
        receiving = "" if st.complete else "  (receiving)"
        self.status_label.setText(
            f"{state}   {fmt_time(position_s)} / {fmt_time(st.total_s)}{receiving}{self._warning}"
        )
        self._refresh_ui()

    def closeEvent(self, event) -> None:
        if not self.maybe_save():
            event.ignore()
            return
        save_window_geometry(self.saveGeometry())
        if self._player is not None:
            self._end_session("")
        super().closeEvent(event)
