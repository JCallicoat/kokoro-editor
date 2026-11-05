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
    QInputDialog,
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
from kokoro_editor.casts_tab import ConflictResolver
from kokoro_editor.characters import (
    MAX_RATE,
    MIN_RATE,
    Character,
    count_voice_tags,
    is_voice_mix,
    merge_characters,
    strip_voice_tags,
    unknown_tag_names,
    unvoiced_tag_names,
    voice_tag,
    voice_tag_pattern,
)
from kokoro_editor.characters_panel import CharacterPickerDialog, PanelWidget
from kokoro_editor.highlighter import TagHighlighter
from kokoro_editor.render_dialog import RenderDialog
from kokoro_editor.settings import (
    REQUEST_INACTIVITY_TIMEOUT_MS,
    AppSettings,
    add_recent_file,
    describe_http_error,
    file_cast,
    fmt_time,
    load_casts,
    load_highlight_tags,
    load_last_dir,
    load_recent_files,
    load_tag_toolbar_visible,
    load_window_geometry,
    remove_recent_file,
    save_file_cast,
    save_highlight_tags,
    save_last_dir,
    save_recent_files,
    save_tag_toolbar_visible,
    save_window_geometry,
    speech_payload,
    speech_request,
)
from kokoro_editor.settings_dialog import SettingsDialog
from kokoro_editor.tag_toolbar import PronunciationDialog, TagToolbar
from kokoro_editor.tags import (
    MAX_PAUSE_S,
    find_tags,
    pause_budget_error,
    pause_tag,
    pronunciation_tag,
    rate_tag,
    tag_at,
    tag_problems,
)
from kokoro_editor.widgets import EditorTextEdit, SeekSlider

APP_NAME = "Kokoro Editor"
UNTITLED = "Untitled"
FILE_FILTER = "Text files (*.txt);;All files (*)"
MAX_LISTED_PROBLEMS = 15


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
        # A file was opened while the voice list was still loading.
        self._validation_waiting = False

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
        self.panel.voices_fetched.connect(self._on_voices_fetched)
        self.panel.set_tag_counter(
            lambda name: count_voice_tags(self.text_edit.toPlainText(), name)
        )
        self.text_edit.character_dropped.connect(self._insert_named_tag)
        self.text_edit.tag_double_clicked.connect(self._edit_tag)
        self.text_edit.tag_tooltip = self._tag_tooltip

        self.highlighter = TagHighlighter(None)
        self._highlighting = load_highlight_tags()
        self.panel.characters_changed.connect(self._refresh_highlighter)

        self.tag_toolbar = TagToolbar()
        self.tag_toolbar.character_clicked.connect(self._insert_named_tag)
        self.tag_toolbar.pause_clicked.connect(self._insert_pause)
        self.tag_toolbar.rate_clicked.connect(self._insert_rate)
        self.tag_toolbar.pronunciation_clicked.connect(
            self._insert_pronunciation
        )
        self.panel.characters_changed.connect(
            lambda: self.tag_toolbar.set_characters(self.panel.characters())
        )
        self.tag_toolbar.setVisible(load_tag_toolbar_visible())
        self._last_pause_s = 1.0
        self._last_rate = 1.0
        self._last_ipa = ""

        # The toolbar sits under the editor, beside the panel.
        editor_column = QVBoxLayout()
        editor_column.addWidget(self.text_edit, 1)
        editor_column.addWidget(self.tag_toolbar)
        top = QHBoxLayout()
        top.addLayout(editor_column, 1)
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
            self._action("Sa&ve Cast", self._save_cast, "Alt+S")
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
        edit_menu.addAction(
            self._action("&Validate Document Tags", self._validate_tags)
        )

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
        self.toolbar_action = self._action(
            "&Tag Toolbar",
            lambda checked: self._show_tag_toolbar(checked),
            "Alt+=",
        )
        self.toolbar_action.setCheckable(True)
        self.toolbar_action.setChecked(self.tag_toolbar.isVisibleTo(self))
        view_menu.addAction(self.toolbar_action)
        self.highlight_action = self._action(
            "&Highlight Tags",
            lambda checked: self._set_highlighting(checked),
            "Alt+Z",
        )
        self.highlight_action.setCheckable(True)
        self.highlight_action.setChecked(self._highlighting)
        view_menu.addAction(self.highlight_action)

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
        self._offer_file_cast(path)
        self._validate_when_voices_known()

    def _validate_when_voices_known(self) -> None:
        """Validate the document quietly once the server's voice list is
        in, so raw voice mixes are checked against it."""
        if self.panel.voices_pending():
            self._validation_waiting = True
        else:
            self._validate_tags(quiet=True)

    def _on_voices_fetched(self) -> None:
        self._refresh_highlighter()  # mixes can now be told from unknowns
        if self._validation_waiting:
            self._validation_waiting = False
            self._validate_tags(quiet=True)

    def _offer_file_cast(self, path: str) -> None:
        """Offer to switch to the cast the file was last saved with, if
        that's another saved cast."""
        cast = file_cast(path)
        current = self.panel.cast_name()
        if not cast or cast == current or cast not in load_casts():
            return
        answer = QMessageBox.question(
            self,
            "Switch Cast",
            f"{os.path.basename(path)} was last saved with the cast {cast}, "
            f"but the current cast is {current or 'unsaved'}.\n\n"
            f"Switch to {cast}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.panel.switch_cast(cast)

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
        save_file_cast(self._path, self.panel.cast_name())
        self.status_label.setText(f"Saved {path}")
        return True

    # -- UI helpers --------------------------------------------------------- #
    def _apply_settings(self) -> None:
        """Push the current settings into the UI (at startup and after Settings)."""
        self.text_edit.setFont(self.settings.editor_font())
        self.panel.set_settings(self.settings)
        self._refresh_highlighter()
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
            self.settings,
            self._nam,
            self._on_preview_active,
            self.panel,
            self,
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

    def _prepare_speech(self) -> str | None:
        """The text Play and Render send, once its voice tags check out,
        or None to send nothing.

        The server rejects a whole request over one tag it can't resolve,
        so unknown tags are offered for replacement, and any left are
        removed from what's sent (the document keeps them).
        """
        text = self._text_to_speak()
        unknown = self._unknown_tags(text)
        if unknown:
            if self._ask_replace_unknown(unknown, sending=True):
                self._replace_unknown_tags(unknown)
                text = self._text_to_speak()
                unknown = self._unknown_tags(text)
            text = strip_voice_tags(text, unknown).strip()
        if not text:
            self.status_label.setText("Nothing to speak.")
            return None
        budget = pause_budget_error(text)
        if budget:
            QMessageBox.warning(self, "Too many pauses", budget)
            return None
        if not self._check_voiced(text):
            return None
        if not self.settings.voice_string:
            self.status_label.setText("Choose a voice first.")
            self._open_settings()
        return text if self.settings.voice_string else None

    def _check_voiced(self, text: str) -> bool:
        """Warn about tagged characters still without a voice, which the
        server can't speak. False if there are any."""
        unvoiced = unvoiced_tag_names(text, self.panel.characters())
        if unvoiced:
            QMessageBox.warning(
                self,
                "Characters without voices",
                "Choose a voice for these characters before speaking:\n\n"
                + ", ".join(unvoiced),
            )
        return not unvoiced

    # -- voice tag checks ---------------------------------------------------- #
    def _unknown_tags(self, text: str) -> list[str]:
        return unknown_tag_names(
            text, self.panel.characters(), self.panel.voices()
        )

    def _validate_tags(self, quiet: bool = False) -> None:
        """Check every tag in the document: offer to replace unknown voice
        tags, then list whatever else won't work as written, selecting the
        first such tag. quiet (when a file is opened) says nothing if all
        is well. Edit > Validate Document Tags."""
        unknown = self._unknown_tags(self.text_edit.toPlainText())
        if unknown and self._ask_replace_unknown(unknown, sending=False):
            self._replace_unknown_tags(unknown)
            unknown = self._unknown_tags(self.text_edit.toPlainText())
        text = self.text_edit.toPlainText()
        lines = []
        if unknown:
            lines.append(
                "Unknown voice tags, left out of what's spoken: "
                + ", ".join(voice_tag(name) for name in unknown)
            )
        unvoiced = unvoiced_tag_names(text, self.panel.characters())
        if unvoiced:
            lines.append(
                "Characters without a voice yet, which can't be spoken: "
                + ", ".join(unvoiced)
            )
        budget = pause_budget_error(text)
        if budget:
            lines.append(budget)
        problems = tag_problems(text)
        for problem in problems[:MAX_LISTED_PROBLEMS]:
            line = text.count("\n", 0, problem.start) + 1
            lines.append(f"Line {line}: {problem.message}")
        if len(problems) > MAX_LISTED_PROBLEMS:
            lines.append(
                f"…and {len(problems) - MAX_LISTED_PROBLEMS} more like these."
            )
        if not lines:
            if not quiet:
                QMessageBox.information(
                    self, "Validate Document Tags", "All tags are valid."
                )
            return
        if problems:
            self._select_text(text, problems[0].start, problems[0].end)
        QMessageBox.warning(
            self,
            "Document Tags",
            "Some tags won't work as written:\n\n"
            + "\n\n".join(lines)
            + ("\n\nThe first is selected." if problems else ""),
        )

    def _select_text(self, text: str, start: int, end: int) -> None:
        """Select text[start:end] in the editor. Qt counts positions in
        UTF-16 code units, which differ from Python's string indices."""

        def position(index: int) -> int:
            return len(text[:index].encode("utf-16-le")) // 2

        cursor = self.text_edit.textCursor()
        cursor.setPosition(position(start))
        cursor.setPosition(position(end), QTextCursor.MoveMode.KeepAnchor)
        self.text_edit.setTextCursor(cursor)

    def _ask_replace_unknown(self, names: list[str], sending: bool) -> bool:
        tags = "\n".join(voice_tag(name) for name in names)
        consequence = (
            "If not, they're left out of what's sent to the server."
            if sending
            else "If not, they're left as they are."
        )
        answer = QMessageBox.question(
            self,
            "Unknown voice tags",
            "These voice tags aren't characters in the current cast or "
            f"Kokoro voices:\n\n{tags}\n\nReplace them with characters? "
            + consequence,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _replace_unknown_tags(self, names: list[str]) -> None:
        """Ask for a character to stand in for each unknown tag name, and
        replace its tags throughout the document. Skipped ones stay."""
        for name in names:
            picked = self._pick_character(
                title="Replace Voice Tag",
                message=f"Replace every {voice_tag(name)} in the document "
                "with:",
                ok_text="Replace",
                cancel_text="Skip",
            )
            if picked is not None:
                count = self._replace_voice_tags(name, picked[0])
                self.status_label.setText(
                    f"Replaced {count} {voice_tag(name)} with "
                    f"{voice_tag(picked[0])}."
                )

    def _pick_character(self, **dialog_options) -> tuple[str, bool] | None:
        """Pick a character from any cast with CharacterPickerDialog (its
        options as given): the name to tag, and whether its option box was
        ticked; None if cancelled. Only the current cast's characters reach
        the server, so one from another cast is imported into it first;
        declining keeps the picker open."""
        picked: list[str] = []

        def adopt(choice: tuple[str | None, str]) -> bool:
            adopted = self._adopt_character(*choice)
            if adopted is not None:
                picked.append(adopted)
            return adopted is not None

        dialog = CharacterPickerDialog(
            self.panel.cast_name(),
            self.panel.characters(),
            self,
            accept_check=adopt,
            **dialog_options,
        )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        option = dialog.option_check.isChecked()
        dialog.deleteLater()
        if not accepted or not picked:
            return None
        return picked[-1], option

    def _adopt_character(self, cast: str | None, name: str) -> str | None:
        """The name to tag for a character picked from cast, importing it
        into the current cast first if it's from another one. None if the
        user declines, keeping the picker open."""
        if cast == self.panel.cast_name():
            return name
        current = self.panel.cast_name() or "the current cast"
        answer = QMessageBox.question(
            self,
            "Import Character",
            f"{name} is in the cast {cast}. Import it into {current} so its "
            "tags can be spoken?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        character = next(
            (c for c in load_casts().get(cast or "", []) if c.name == name),
            None,
        )
        if answer != QMessageBox.StandardButton.Yes or character is None:
            return None
        resolver = ConflictResolver(
            self.panel.cast_name() or "(unsaved)", self, allow_apply_all=False
        )
        merged, report = merge_characters(
            self.panel.characters(), [character], resolver
        )
        if resolver.cancelled:
            return None
        self.panel.set_characters(merged)
        if report.renamed:
            return report.renamed[0]
        # Added, overwritten, or kept: the current cast's one answers to it.
        existing = next(
            c for c in merged if c.name.casefold() == character.name.casefold()
        )
        return existing.name

    def _rename_voice_tags(self, old: str, new: str) -> None:
        count = self._replace_voice_tags(old, new)
        if count:
            tags = "tag" if count == 1 else "tags"
            self.status_label.setText(
                f"Renamed {count} {tags} from {old} to {new}."
            )

    def _replace_voice_tags(self, old: str, new: str) -> int:
        """Point the document's tags for old at new, as a single undo step.
        Returns how many were replaced."""
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
        return count

    def insert_voice_tag(self, character: Character) -> None:
        """Insert character's [voice:...] tag at the cursor. Every way of
        inserting a character tag goes through here."""
        self.insert_tag(voice_tag(character.name))

    def insert_tag(self, tag: str) -> None:
        """Insert tag at the cursor (the start of any selection), spaced
        off from text it would touch, and return focus to the editor."""
        self.text_edit.insert_tag(tag)

    # -- tags already in the text -------------------------------------------- #
    def _tag_tooltip(self, text: str, index: int) -> str | None:
        """What hovering the text at index says: a problem with a tag, or
        what the tag does."""
        for problem in tag_problems(text):
            if problem.start <= index < problem.end:
                return problem.message
        tag = tag_at(text, index)
        if tag is None:
            return None
        hint = "\n\nDouble-click to change it; drag it to move it."
        if tag.kind == "voice":
            character = self._character_named(tag.value)
            if character is not None:
                return character.tooltip() + hint
            if is_voice_mix(tag.value, self.panel.voices()):
                return f"Kokoro voice mix: {tag.value}" + hint
            return (
                f"{voice_tag(tag.value)} isn't a character in the current "
                "cast or a Kokoro voice, so the server would refuse it." + hint
            )
        if tag.kind == "pause":
            return f"A pause of {tag.value} seconds." + hint
        if tag.kind == "pronunciation":
            return f"“{tag.value}” said as /{tag.ipa}/." + hint
        what = "Rate" if tag.kind == "rate" else "Base rate"
        return f"{what} {tag.value}× until the next rate or voice tag." + hint

    def _character_named(self, name: str) -> Character | None:
        """The current cast's character called name, ignoring case as the
        server does."""
        for character in self.panel.characters():
            if character.name.casefold() == name.casefold():
                return character
        return None

    def _edit_tag(self, start: int, end: int) -> None:
        """Double-clicking a tag: choose another character for a voice tag,
        or a new value for the others, replacing just that tag (or, if
        chosen, every tag for the same character)."""
        cursor = QTextCursor(self.text_edit.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        (tag,) = find_tags(cursor.selectedText())
        replacement = None
        if tag.kind == "voice":
            choice = self._choose_replacement_character(tag.value)
            if choice is not None:
                name, everywhere = choice
                if everywhere:
                    count = self._replace_voice_tags(tag.value, name)
                    self.status_label.setText(
                        f"Changed {count} {voice_tag(tag.value)} to "
                        f"{voice_tag(name)}."
                    )
                else:
                    replacement = voice_tag(name)
        elif tag.kind == "pause":
            seconds, ok = QInputDialog.getDouble(
                self,
                "Change Pause",
                "Pause length in seconds:",
                float(tag.value),
                0.05,
                MAX_PAUSE_S,
                2,
            )
            if ok:
                replacement = pause_tag(seconds)
        elif tag.kind in ("rate", "baserate"):
            rate, ok = QInputDialog.getDouble(
                self,
                "Change Rate",
                "Speaking rate until the next rate or voice tag:",
                float(tag.value),
                MIN_RATE,
                MAX_RATE,
                2,
            )
            if ok:
                replacement = f"[{tag.kind}:{rate:g}]"
        else:
            dialog = PronunciationDialog(tag.value, tag.ipa, self)
            dialog.setWindowTitle("Change Pronunciation")
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            word, ipa = dialog.values()
            dialog.deleteLater()
            if accepted:
                replacement = pronunciation_tag(word, ipa)
        if replacement is not None:
            cursor.insertText(replacement)  # exactly in the tag's place
            self.text_edit.setTextCursor(cursor)
        self.text_edit.setFocus()

    def _choose_replacement_character(
        self, name: str
    ) -> tuple[str, bool] | None:
        """Ask which character a voice tag should be: (name to tag,
        whether to change every tag for name), or None if cancelled."""
        return self._pick_character(
            title="Change Character",
            message=f"Change {voice_tag(name)} to:",
            ok_text="Change",
            option_text=f"Change every {voice_tag(name)} in the document",
        )

    def _insert_pause(self) -> None:
        seconds, ok = QInputDialog.getDouble(
            self,
            "Insert Pause",
            "Pause length in seconds:",
            self._last_pause_s,
            0.05,
            MAX_PAUSE_S,
            2,
        )
        if ok:
            self._last_pause_s = seconds
            self.insert_tag(pause_tag(seconds))
        self.text_edit.setFocus()

    def _insert_rate(self) -> None:
        rate, ok = QInputDialog.getDouble(
            self,
            "Insert Rate Change",
            "Speaking rate until the next rate or voice tag (1 is the "
            "voice's own):",
            self._last_rate,
            MIN_RATE,
            MAX_RATE,
            2,
        )
        if ok:
            self._last_rate = rate
            self.insert_tag(rate_tag(rate))
        self.text_edit.setFocus()

    def _insert_pronunciation(self) -> None:
        """Ask for a word's pronunciation; a selected word is replaced by
        its tag, otherwise the tag goes in at the cursor."""
        cursor = self.text_edit.textCursor()
        # selectedText() separates paragraphs with U+2029.
        selected = cursor.selectedText().replace("\u2029", " ").strip()
        dialog = PronunciationDialog(selected, self._last_ipa, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        word, ipa = dialog.values()
        dialog.deleteLater()
        if accepted:
            self._last_ipa = ipa
            tag = pronunciation_tag(word, ipa)
            if selected:
                cursor.insertText(tag)
                self.text_edit.setTextCursor(cursor)
            else:
                self.insert_tag(tag)
        self.text_edit.setFocus()

    # -- tag highlighting --------------------------------------------------- #
    def _refresh_highlighter(self) -> None:
        if not self._highlighting:
            return
        if self.highlighter.document() is None:
            self.highlighter.setDocument(self.text_edit.document())
        self.highlighter.update(
            self.panel.characters(),
            self.panel.colors(),
            self.panel.voices(),
            self.settings,
        )

    def _set_highlighting(self, highlight: bool) -> None:
        """View > Highlight Tags. Off, the highlighter is detached so the
        text goes back to plain."""
        self._highlighting = highlight
        save_highlight_tags(highlight)
        if highlight:
            self._refresh_highlighter()
        else:
            self.highlighter.setDocument(None)

    def _save_cast(self) -> None:
        """File > Save Cast, like the panel's save button: a cast not
        saved yet is asked for a name."""
        if self.panel.save_cast():
            self.status_label.setText(
                f"Saved the cast {self.panel.cast_name()}."
            )

    def _show_tag_toolbar(self, show: bool) -> None:
        self.tag_toolbar.setVisible(show)
        save_tag_toolbar_visible(show)

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
        """Alt+0: insert the tag of a character picked from any cast,
        importing it into the current cast if it's from another."""
        picked = self._pick_character()
        if picked is not None:
            self._insert_named_tag(picked[0])

    def _on_render_clicked(self) -> None:
        text = self._prepare_speech()
        if text is None:
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
        text = self._prepare_speech()
        if text is None:
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
