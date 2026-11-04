"""Main editor window: text editing, file handling and speech playback."""

from __future__ import annotations

import json
import os

from PySide6.QtCore import (
    QByteArray,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    QSize,
    QTimer,
    QUrl,
)
from PySide6.QtGui import QAction, QCursor, QKeySequence, QPainter
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QFormLayout,
    QGroupBox,
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
    AppSettings,
    add_recent_file,
    build_voice_string,
    describe_http_error,
    fmt_time,
    load_last_dir,
    load_recent_files,
    load_window_geometry,
    remove_recent_file,
    save_last_dir,
    save_recent_files,
    save_window_geometry,
    speech_payload,
    speech_request,
)
from kokoro_editor.settings_dialog import SettingsDialog
from kokoro_editor.widgets import SeekSlider

APP_NAME = "Kokoro Editor"
UNTITLED = "Untitled"
FILE_FILTER = "Text files (*.txt);;All files (*)"
REQUEST_INACTIVITY_TIMEOUT_MS = (
    120_000  # Qt 6 defaults to 30 s, too short for slow first chunks
)


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


class PanelWidget(QWidget):
    """Collapsible side panel with a Characters label and Add/Remove buttons."""

    COLLAPSED_WIDTH = 30
    EXPANDED_WIDTH = 220

    def __init__(
        self, settings: AppSettings, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setFixedWidth(self.EXPANDED_WIDTH)
        self._settings = settings

        self._collapsed = True
        self._nam = QNetworkAccessManager(self)
        self._voices: list[str] = []
        self._reply: QNetworkReply | None = None
        self._active_play_btn: QPushButton | None = None
        self._last_active_btn: QPushButton | None = None
        self._example_player: PCMPlayer | None = None

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
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
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

    def _parse_voice_string(self, voice_str: str) -> list[tuple[str, int]]:
        """Parse 'af_bella(2)+am_michael' into [('af_bella', 2), ('am_michael', 1)]."""
        result: list[tuple[str, int]] = []
        for part in voice_str.split("+"):
            part = part.strip()
            if "(" in part:
                name, weight_str = part.rsplit("(", 1)
                name = name.strip()
                weight = int(weight_str.rstrip(")"))
            else:
                name = part
                weight = 1
            result.append((name, weight))
        return result

    def _fetch_voices(self) -> None:
        self._cancel_fetch()
        url = QUrl(self._settings.base_url.rstrip("/") + "/v1/audio/voices")
        request = QNetworkRequest(url)
        try:
            request.setTransferTimeout(10_000)
        except Exception:
            pass
        reply = self._nam.get(request)
        self._reply = reply
        reply.finished.connect(lambda r=reply: self._on_voices(r))

    def _cancel_fetch(self) -> None:
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.abort()

    def _on_voices(self, reply: QNetworkReply) -> None:
        if reply is not self._reply:
            reply.deleteLater()
            return
        self._reply = None
        if reply.error() == QNetworkReply.NetworkError.NoError:
            try:
                import json

                data = json.loads(bytes(reply.readAll().data()))
                items = (
                    data.get("voices", []) if isinstance(data, dict) else data
                )
                self._voices = []
                for item in items:
                    if isinstance(item, str):
                        self._voices.append(item)
                    elif isinstance(item, dict):
                        name = (
                            item.get("id")
                            or item.get("name")
                            or item.get("voice_id")
                        )
                        if name:
                            self._voices.append(str(name))
                self._voices = sorted(set(self._voices))
            except (ValueError, TypeError, AttributeError):
                pass
        reply.deleteLater()

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
        dialog = QDialog(self)
        dialog.setWindowTitle(
            "Edit Character" if editing_row >= 0 else "Add Character"
        )
        dialog.setMinimumWidth(360)
        layout = QVBoxLayout(dialog)

        # Character name
        form = QFormLayout()
        self._name_edit = QLineEdit()
        self._name_edit.setText(initial_name)
        form.addRow("Character:", self._name_edit)
        layout.addLayout(form)

        # Voice selection
        voice_group = QGroupBox("Voice mix")
        voice_layout = QFormLayout(voice_group)
        self._voice_boxes: list[QComboBox] = []
        self._weight_boxes: list[QComboBox] = []
        self._voice_play_btns: list[QPushButton] = []
        for i in range(3):
            label = "required" if i == 0 else "optional"
            row = QHBoxLayout()
            voice_box = QComboBox()
            voice_box.addItem("(none)", "")
            for v in self._voices:
                voice_box.addItem(v, v)
            self._voice_boxes.append(voice_box)
            weight_box = QComboBox()
            for w in range(1, 11):
                weight_box.addItem(str(w), w)
            weight_box.setCurrentIndex(0)
            self._weight_boxes.append(weight_box)
            row.addWidget(voice_box, 1)
            row.addWidget(weight_box)
            play_btn = QPushButton()
            play_btn.setFlat(True)
            play_btn.setFixedWidth(24)
            play_btn.setIcon(
                self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
            )
            play_btn.clicked.connect(
                lambda _=False, i=i: self._play_example_voice(i)
            )
            row.addWidget(play_btn)
            self._voice_play_btns.append(play_btn)
            voice_layout.addRow(f"Voice {i + 1} ({label})", row)

        self._voice_string_edit = QLineEdit()
        self._voice_string_edit.setReadOnly(True)
        self._voice_string_edit.setStyleSheet("color: gray;")
        self._voice_mix_play_btn = QPushButton()
        self._voice_mix_play_btn.setFlat(True)
        self._voice_mix_play_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        self._voice_mix_play_btn.clicked.connect(self._play_mixed_voice)
        row2 = QHBoxLayout()
        row2.addWidget(self._voice_string_edit, 1)
        row2.addWidget(self._voice_mix_play_btn)
        voice_layout.addRow("Resulting voice", row2)

        layout.addWidget(voice_group)

        # Populate from initial values when editing
        if initial_voice:
            parsed = self._parse_voice_string(initial_voice)
            for i, (name, weight) in enumerate(parsed):
                if i < len(self._voice_boxes):
                    self._voice_boxes[i].setCurrentText(name)
                    self._weight_boxes[i].setCurrentText(str(weight))

        # Connect voice changes to update the computed string
        for box in self._voice_boxes + self._weight_boxes:
            box.currentIndexChanged.connect(self._update_dialog_voice_string)
        self._update_dialog_voice_string()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            name = self._name_edit.text().strip()
            if not name:
                return
            voices = [b.currentData() or "" for b in self._voice_boxes]
            weights = [b.currentData() or 1 for b in self._weight_boxes]
            voice_str = build_voice_string(voices, weights)
            if editing_row >= 0:
                self.table.setItem(editing_row, 0, QTableWidgetItem(name))
                self.table.setItem(editing_row, 1, QTableWidgetItem(voice_str))
            else:
                row = self.table.rowCount()
                self.table.insertRow(row)
                self.table.setItem(row, 0, QTableWidgetItem(name))
                self.table.setItem(row, 1, QTableWidgetItem(voice_str))
            self.table.resizeRowsToContents()
            self.table.scrollToBottom()

    def _update_dialog_voice_string(self) -> None:
        voices = [b.currentData() or "" for b in self._voice_boxes]
        weights = [b.currentData() or 1 for b in self._weight_boxes]
        self._voice_string_edit.setText(build_voice_string(voices, weights))

    # -- example voice playback --------------------------------------------- #
    def _play_example_voice(self, index: int) -> None:
        voice_name = self._voice_boxes[index].currentData()
        if not voice_name:
            return
        self._play_example(
            f"Hello! I am {voice_name} voice provided by the kokoro API. This is how I sound.",
            voice_name,
            self._voice_play_btns[index],
        )

    def _play_mixed_voice(self) -> None:
        voice_str = self._voice_string_edit.text()
        if not voice_str:
            return
        self._play_example(
            "Hello! I am the mixed voice provided by the kokoro API. This is how I sound.",
            voice_str,
            self._voice_mix_play_btn,
        )

    def _play_example(
        self, text: str, voice: str, btn: QPushButton | None = None
    ) -> None:
        # Pause main window player if playing
        self._parent = None
        # Walk up the parent chain to find the MainWindow (parent is PanelWidget)
        parent = self.parent()
        main_window = None
        while parent is not None:
            if hasattr(parent, "_player") and parent._player is not None:
                main_window = parent
                break
            parent = parent.parent()
        if main_window is not None:
            main_window._player.set_paused(True)
            self._parent = main_window

        # If something is already playing
        if self._is_example_playing():
            old_btn = self._active_play_btn
            self._reply = None
            # Cancel the polling timer
            if hasattr(self, "_check_timer") and self._check_timer is not None:
                self._check_timer.stop()
            # Reset old button icon to play
            if old_btn is not None:
                old_btn.setIcon(
                    self.style().standardIcon(
                        QStyle.StandardPixmap.SP_MediaPlay
                    )
                )
            # If clicking the same button (or the last-active button after
            # audio finished), just stop (resume main player)
            if btn is old_btn or btn is self._last_active_btn:
                self._active_play_btn = None
                self._last_active_btn = None
                # Actually stop playback by closing and resetting the stream
                if self._example_player is not None:
                    self._example_player.close(drain=False)
                    self._example_player = None
                # Reset button icon to play
                btn.setIcon(
                    self.style().standardIcon(
                        QStyle.StandardPixmap.SP_MediaPlay
                    )
                )
                if main_window is not None:
                    main_window._player.set_paused(False)
                return
            # Otherwise switch to new voice
            self._active_play_btn = btn

        self._active_play_btn = btn
        self._last_active_btn = btn
        self._set_example_playing(True)

        from kokoro_editor.settings import speech_payload, speech_request

        s = self._settings
        if self._example_player is None:
            self._example_player = PCMPlayer(s.sample_rate, s.prebuffer_ms)
            try:
                self._example_player.open()
            except Exception:
                self._set_example_playing(False)
                self._active_play_btn = None
                if main_window is not None:
                    main_window._player.set_paused(False)
                return

        # Reset buffer for new playback
        self._example_player.reset()

        payload = speech_payload(s, text, "pcm", stream=True, voice=voice)
        request = speech_request(s.base_url, 120_000)
        reply = self._nam.post(
            request, QByteArray(json.dumps(payload).encode("utf-8"))
        )

        player = self._example_player

        def _on_ready() -> None:
            if reply is not self._reply:
                return
            data = bytes(reply.readAll().data())
            if not data:
                return
            status = reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute
            )
            if status is not None and status >= 400:
                return
            player.feed(data)

        def _on_finished() -> None:
            if reply is not self._reply:
                return
            self._reply = None
            status = reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute
            )
            if status is not None and status >= 400:
                return
            leftover = bytes(reply.readAll().data())
            if leftover:
                player.feed(leftover)
            player.mark_complete()

        reply.readyRead.connect(_on_ready)
        reply.finished.connect(_on_finished)
        self._reply = reply

        # Poll player state to reset icon when playback actually finishes
        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(200)
        self._check_timer.timeout.connect(
            lambda: self._check_playback_finished(player)
        )
        self._check_timer.start()

    def _check_playback_finished(self, player: PCMPlayer) -> None:
        """Check if player has finished and reset the button icon."""
        state = player.snapshot()
        if state.finished or (
            state.complete and not state.primed and state.total_s == 0
        ):
            self._set_example_playing(False)
            self._active_play_btn = None
            # Resume main window player
            if (
                self._parent
                and hasattr(self._parent, "_player")
                and self._parent._player is not None
            ):
                self._parent._player.set_paused(False)
            # Don't clear _last_active_btn so stop-click detection works
        # Continue polling if audio hasn't finished
        if not state.finished:
            self._check_timer = QTimer(self)
            self._check_timer.setSingleShot(True)
            self._check_timer.setInterval(200)
            self._check_timer.timeout.connect(
                lambda: self._check_playback_finished(player)
            )
            self._check_timer.start()

    def _is_example_playing(self) -> bool:
        return self._reply is not None or self._active_play_btn is not None

    def _set_example_playing(self, playing: bool) -> None:
        if self._active_play_btn is None:
            return
        icon = (
            QStyle.StandardPixmap.SP_MediaStop
            if playing
            else QStyle.StandardPixmap.SP_MediaPlay
        )
        self._active_play_btn.setIcon(self.style().standardIcon(icon))

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

        self.panel = PanelWidget(self.settings, self)

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
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings = dialog.result_settings()
            self.settings.save()
            self._apply_settings()

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
