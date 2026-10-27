"""Main editor window: text editing, file handling and speech playback."""

from __future__ import annotations

import json
import os

from PySide6.QtCore import QByteArray, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from kokoro_editor import audio
from kokoro_editor.audio import PCMPlayer
from kokoro_editor.render_dialog import RenderDialog
from kokoro_editor.settings import (
    AppSettings,
    describe_http_error,
    fmt_time,
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


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
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

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.text_edit, 1)
        layout.addLayout(controls)
        layout.addWidget(self.seek_slider)
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)

        self._create_menus()
        self._update_title()
        self._update_summary()
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
        file_menu.addAction(self._action("&Open…", self.open_document, Key.Open))
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
        self.stop_action = self._action("&Stop", self._on_stop_clicked, "Ctrl+.")
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
        return os.path.dirname(self._path) if self._path else os.getcwd()

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
            QMessageBox.critical(self, "Could not open file", f"{path}\n\n{exc}")
            return
        self._set_document(text, path)
        self.status_label.setText(f"Opened {path}")

    def save(self) -> bool:
        if self._path is None:
            return self.save_as()
        return self._write(self._path)

    def save_as(self) -> bool:
        start = self._path or os.path.join(self._dialog_dir(), f"{UNTITLED}.txt")
        path, _ = QFileDialog.getSaveFileName(self, "Save As", start, FILE_FILTER)
        if not path:
            return False
        return self._write(path)

    def _write(self, path: str) -> bool:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.text_edit.toPlainText())
        except OSError as exc:
            QMessageBox.critical(self, "Could not save file", f"{path}\n\n{exc}")
            return False
        self._path = path
        self.text_edit.document().setModified(False)
        self._update_title()
        self.status_label.setText(f"Saved {path}")
        return True

    # -- UI helpers --------------------------------------------------------- #
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
            self._update_summary()

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
        if self._path:
            directory = os.path.dirname(self._path)
            stem = os.path.splitext(os.path.basename(self._path))[0]
        else:
            directory, stem = os.getcwd(), UNTITLED
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
        data = reply.readAll().data()
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
        leftover = reply.readAll().data()
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
        if self._player is not None:
            self._end_session("")
        super().closeEvent(event)
