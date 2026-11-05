"""Main editor window: text editing, file handling and speech playback."""

from __future__ import annotations

import json
import os

from PySide6.QtCore import (
    QByteArray,
    QRegularExpression,
    Qt,
    QTimer,
)
from PySide6.QtGui import QAction, QKeySequence, QTextCursor
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
    QPushButton,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from kokoro_editor import audio
from kokoro_editor.audio import PCMPlayer
from kokoro_editor.characters import (
    Character,
    unvoiced_tag_names,
    voice_tag,
    voice_tag_pattern,
)
from kokoro_editor.characters_panel import CharacterPickerDialog, PanelWidget
from kokoro_editor.render_dialog import RenderDialog
from kokoro_editor.settings import (
    REQUEST_INACTIVITY_TIMEOUT_MS,
    AppSettings,
    add_recent_file,
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
from kokoro_editor.widgets import EditorTextEdit, SeekSlider

APP_NAME = "Kokoro Editor"
UNTITLED = "Untitled"
FILE_FILTER = "Text files (*.txt);;All files (*)"


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
        self.text_edit = EditorTextEdit()
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
        self.panel.character_renamed.connect(self._rename_voice_tags)
        self.text_edit.character_dropped.connect(self._insert_named_tag)

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
        edit_menu.addSeparator()
        # Alt+1…9 insert the cast's first characters. They're window actions
        # so the keys are always taken, even past the last character (which
        # would otherwise type the digit), and the menu, rebuilt as the
        # characters change, only lists them.
        for number in range(1, 10):
            self.addAction(
                self._action(
                    "",
                    lambda _=False, n=number: self._insert_tag_number(n),
                    f"Alt+{number}",
                )
            )
        self.insert_tag_menu = edit_menu.addMenu("Insert Character &Tag")
        self._choose_character_action = self._action(
            "&Choose Character…", self._choose_character, "Alt+0"
        )
        self.panel.characters_changed.connect(self._update_insert_tag_menu)
        self._update_insert_tag_menu()

        view_menu = bar.addMenu("&View")
        self.panel_action = self._action(
            "&Characters Panel",
            lambda checked: self._show_panel(checked),
            "Alt+-",
        )
        self.panel_action.setCheckable(True)
        self.panel_action.setChecked(not self.panel.is_collapsed())
        # Follows the panel's own toggle button too.
        self.panel.collapsed_changed.connect(
            lambda collapsed: self.panel_action.setChecked(not collapsed)
        )
        view_menu.addAction(self.panel_action)

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
        # The server rejects the whole request over a tag it can't resolve.
        unvoiced = unvoiced_tag_names(text, self.panel.characters())
        if unvoiced:
            QMessageBox.warning(
                self,
                "Characters without voices",
                "Choose a voice for these characters before speaking:\n\n"
                + ", ".join(unvoiced),
            )
            return False
        if not self.settings.voice_string:
            self.status_label.setText("Choose a voice first.")
            self._open_settings()
        return bool(self.settings.voice_string)

    def _rename_voice_tags(self, old: str, new: str) -> None:
        """Point the document's tags for a renamed character at its new
        name, as a single undo step."""
        document = self.text_edit.document()
        pattern = QRegularExpression(
            voice_tag_pattern(old),
            QRegularExpression.PatternOption.CaseInsensitiveOption,
        )
        # QTextDocument.find works in Qt's own positions, which differ from
        # Python string indices once the text has characters beyond U+FFFF.
        block = QTextCursor(document)
        block.beginEditBlock()
        count = 0
        found = document.find(pattern, 0)
        while not found.isNull():
            found.insertText(voice_tag(new))
            count += 1
            found = document.find(pattern, found.position())
        block.endEditBlock()
        if count:
            tags = "tag" if count == 1 else "tags"
            self.status_label.setText(
                f"Renamed {count} {tags} from {old} to {new}."
            )

    def insert_voice_tag(self, character: Character) -> None:
        """Insert character's [voice:...] tag at the cursor (the start of
        any selection), spaced off from the words around it. Every way of
        inserting a character tag goes through here."""
        cursor = self.text_edit.textCursor()
        position = cursor.selectionStart()
        cursor.setPosition(position)
        document = self.text_edit.document()
        before = document.characterAt(position - 1) if position > 0 else ""
        after = document.characterAt(position)
        tag = voice_tag(character.name)
        if before and not before.isspace():
            tag = " " + tag
        # At a line or document end, after is U+2029, which still wants a
        # space so typing can carry on after the tag.
        if after not in (" ", "\t"):
            tag += " "
        cursor.insertText(tag)
        self.text_edit.setTextCursor(cursor)
        self.text_edit.setFocus()

    def _insert_named_tag(self, name: str) -> None:
        character = self.panel.character_named(name)
        if character is not None:
            self.insert_voice_tag(character)

    def _show_panel(self, show: bool) -> None:
        """Expand the characters panel with its table focused for keyboard
        use, or collapse it and go back to the editor."""
        self.panel.set_collapsed(not show)
        if show:
            self.panel.table.setFocus()
        else:
            self.text_edit.setFocus()

    def _insert_tag_number(self, number: int) -> None:
        characters = self.panel.characters()
        if number <= len(characters):
            self.insert_voice_tag(characters[number - 1])
        else:
            self.status_label.setText(f"No character {number} in this cast.")

    def _update_insert_tag_menu(self) -> None:
        menu = self.insert_tag_menu
        menu.clear()
        for number, character in enumerate(self.panel.characters()[:9], 1):
            # Escape & so Qt doesn't treat it as a mnemonic marker. The
            # shortcut belongs to the window action and is only shown here.
            name = character.name.replace("&", "&&")
            action = menu.addAction(f"&{number}  {name}\tAlt+{number}")
            action.triggered.connect(
                lambda _=False, n=number: self._insert_tag_number(n)
            )
        if menu.isEmpty():
            menu.addAction("No characters").setEnabled(False)
        menu.addSeparator()
        menu.addAction(self._choose_character_action)

    def _choose_character(self) -> None:
        dialog = CharacterPickerDialog(
            self.panel.cast_name(), self.panel.characters(), self
        )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        choice = dialog.choice()
        dialog.deleteLater()
        if not accepted or choice is None:
            return
        cast, name = choice
        # Only the current cast's characters reach the server, so a pick
        # from another cast switches to it.
        if cast is not None and not self.panel.switch_cast(cast):
            return
        self._insert_named_tag(name)

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
            self.panel.voice_aliases(),
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

        payload = speech_payload(
            s,
            text,
            "pcm",
            stream=True,
            voice_aliases=self.panel.voice_aliases(),
        )
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
        if not self.maybe_save() or not self.panel.maybe_save_cast():
            event.ignore()
            return
        save_window_geometry(self.saveGeometry())
        if self._player is not None:
            self._end_session("")
        super().closeEvent(event)
