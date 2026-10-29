"""Settings dialog: a tab for the editor and one for the Kokoro server."""

from __future__ import annotations

import json
from dataclasses import replace

from PySide6.QtCore import QByteArray, QTimer
from PySide6.QtGui import QFont
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from kokoro_editor.settings import (
    MAX_FONT_SIZE,
    MAX_VOICE_WEIGHT,
    MIN_FONT_SIZE,
    VOICE_SLOTS,
    AppSettings,
    api_url,
    build_voice_string,
    extract_voice_names,
)


class SettingsDialog(QDialog):
    def __init__(
        self, settings: AppSettings, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)
        self._settings = settings
        self._nam = QNetworkAccessManager(self)
        self._reply: QNetworkReply | None = None
        self._active_play_btn: QPushButton | None = None
        self._last_active_btn: QPushButton | None = None
        self._example_player: "PCMPlayer | None" = None

        # One tab per area; add new tabs by writing another _build_*_tab().
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_editor_tab(settings), "Editor")
        self.kokoro_tab = self._build_kokoro_tab(settings)
        self.tabs.addTab(self.kokoro_tab, "Kokoro")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(buttons)

        # Start with the saved selection so the mix is valid even if the server is down.
        self._set_voice_choices([], list(settings.voices), keep_missing=True)
        for box in self.voice_boxes:
            box.currentIndexChanged.connect(self._update_voice_string)
        self._fetch_voices()

    # -- tabs --------------------------------------------------------------- #
    def _build_editor_tab(self, settings: AppSettings) -> QWidget:
        font = settings.editor_font()
        self.font_box = QFontComboBox()
        self.font_box.setCurrentFont(font)
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(MIN_FONT_SIZE, MAX_FONT_SIZE)
        self.font_size_spin.setSuffix(" pt")
        self.font_size_spin.setValue(font.pointSize())
        self.font_preview = QLabel(
            "The quick brown fox jumps over the lazy dog."
        )
        self.font_preview.setWordWrap(True)
        self.font_preview.setMinimumHeight(48)
        self.font_box.currentFontChanged.connect(self._update_font_preview)
        self.font_size_spin.valueChanged.connect(self._update_font_preview)
        self._update_font_preview()

        font_group = QGroupBox("Font")
        font_form = QFormLayout(font_group)
        font_form.addRow("Family", self.font_box)
        font_form.addRow("Size", self.font_size_spin)
        font_form.addRow(self.font_preview)

        # Restore session checkbox
        self.restore_check = QCheckBox("Restore previous session on startup")
        self.restore_check.setChecked(settings.restore_session)

        editor_group = QGroupBox("Editor")
        editor_layout = QVBoxLayout(editor_group)
        editor_layout.addWidget(self.restore_check)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(font_group)
        layout.addWidget(editor_group)
        layout.addStretch(1)
        return tab

    def _build_kokoro_tab(self, settings: AppSettings) -> QWidget:
        # Server
        self.url_edit = QLineEdit(settings.base_url)
        self.model_edit = QLineEdit(settings.model)
        server_form = QFormLayout()
        server_form.addRow("Server URL", self.url_edit)
        server_form.addRow("Model", self.model_edit)

        # Voice mix
        self.voice_boxes = [QComboBox() for _ in range(VOICE_SLOTS)]
        self.weight_boxes = [QComboBox() for _ in range(VOICE_SLOTS)]
        for box, weight in zip(self.weight_boxes, settings.weights):
            for value in range(1, MAX_VOICE_WEIGHT + 1):
                box.addItem(str(value), value)
            box.setCurrentIndex(max(box.findData(weight), 0))
            box.setToolTip("Mixing weight relative to the other voices")
            box.currentIndexChanged.connect(self._update_voice_string)
        self.voice_string_edit = QLineEdit()
        self.voice_string_edit.setReadOnly(True)
        self._voice_play_btns: list[QPushButton] = []
        self.refresh_btn = QPushButton("Reload voices")
        self.refresh_btn.clicked.connect(self._fetch_voices)
        self.voice_status = QLabel("")
        status_row = QHBoxLayout()
        status_row.addWidget(self.refresh_btn)
        status_row.addWidget(self.voice_status, 1)

        voice_group = QGroupBox("Voice mix")
        voice_form = QFormLayout(voice_group)
        for index, (voice_box, weight_box) in enumerate(
            zip(self.voice_boxes, self.weight_boxes)
        ):
            row = QHBoxLayout()
            row.addWidget(voice_box, 1)
            row.addWidget(weight_box)
            play_btn = QPushButton()
            play_btn.setFlat(True)
            play_btn.setFixedWidth(24)
            play_btn.setIcon(
                self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
            )
            play_btn.clicked.connect(
                lambda _=False, i=index: self._play_example_voice(i)
            )
            row.addWidget(play_btn)
            self._voice_play_btns.append(play_btn)
            label = "required" if index == 0 else "optional"
            voice_form.addRow(f"Voice {index + 1} ({label})", row)
        self._voice_mix_play_btn = QPushButton()
        self._voice_mix_play_btn.setFlat(True)
        self._voice_mix_play_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        self._voice_mix_play_btn.clicked.connect(self._play_mixed_voice)
        row2 = QHBoxLayout()
        row2.addWidget(self.voice_string_edit, 1)
        row2.addWidget(self._voice_mix_play_btn)
        voice_form.addRow("Resulting voice", row2)
        voice_form.addRow(status_row)

        # Speech parameters
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.25, 4.0)
        self.speed_spin.setSingleStep(0.05)
        self.speed_spin.setDecimals(2)
        self.speed_spin.setValue(settings.speed)
        self.volume_spin = QDoubleSpinBox()
        self.volume_spin.setRange(0.1, 20.0)
        self.volume_spin.setSingleStep(0.1)
        self.volume_spin.setDecimals(2)
        self.volume_spin.setValue(settings.volume_multiplier)
        speech_group = QGroupBox("Speech")
        speech_form = QFormLayout(speech_group)
        speech_form.addRow("Speed", self.speed_spin)
        speech_form.addRow("Volume multiplier (server-side)", self.volume_spin)

        # Audio
        self.rate_spin = QSpinBox()
        self.rate_spin.setRange(8000, 96000)
        self.rate_spin.setSingleStep(1000)
        self.rate_spin.setSuffix(" Hz")
        self.rate_spin.setValue(settings.sample_rate)
        self.prebuffer_spin = QSpinBox()
        self.prebuffer_spin.setRange(0, 5000)
        self.prebuffer_spin.setSingleStep(50)
        self.prebuffer_spin.setSuffix(" ms")
        self.prebuffer_spin.setValue(settings.prebuffer_ms)
        audio_group = QGroupBox("Playback")
        audio_form = QFormLayout(audio_group)
        audio_form.addRow("PCM sample rate", self.rate_spin)
        audio_form.addRow("Start-up buffer", self.prebuffer_spin)
        self.scrub_spin = QSpinBox()
        self.scrub_spin.setRange(1, 120)
        self.scrub_spin.setSuffix(" s")
        self.scrub_spin.setValue(settings.scrub_step_s)
        audio_form.addRow("Scrub step (Alt+Left/Right)", self.scrub_spin)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addLayout(server_form)
        layout.addWidget(voice_group)
        layout.addWidget(speech_group)
        layout.addWidget(audio_group)
        layout.addStretch(1)
        return tab

    # -- editor ------------------------------------------------------------- #
    def _selected_font(self) -> QFont:
        font = QFont(self.font_box.currentFont())
        font.setPointSize(self.font_size_spin.value())
        return font

    def _update_font_preview(self) -> None:
        self.font_preview.setFont(self._selected_font())

    # -- voices ------------------------------------------------------------- #
    def _current_selection(self) -> list[str]:
        return [box.currentData() or "" for box in self.voice_boxes]

    def _current_weights(self) -> list[int]:
        return [box.currentData() or 1 for box in self.weight_boxes]

    def _set_voice_choices(
        self, voices: list[str], selected: list[str], keep_missing: bool
    ) -> None:
        for index, box in enumerate(self.voice_boxes):
            box.blockSignals(True)
            box.clear()
            if index > 0:
                box.addItem("(none)", "")
            names = list(voices)
            wanted = selected[index]
            if wanted and wanted not in names and keep_missing:
                names.append(wanted)
            for name in names:
                box.addItem(name, name)
            box.setCurrentIndex(max(box.findData(wanted), 0))
            box.blockSignals(False)
        self._update_voice_string()

    def _update_voice_string(self) -> None:
        selection = self._current_selection()
        for voice, weight_box in zip(selection, self.weight_boxes):
            weight_box.setEnabled(bool(voice))
        self.voice_string_edit.setText(
            build_voice_string(selection, self._current_weights())
        )

    def _fetch_voices(self) -> None:
        self._cancel_fetch()
        request = QNetworkRequest(
            api_url(self.url_edit.text(), "/v1/audio/voices")
        )
        try:
            request.setTransferTimeout(10_000)
        except Exception:
            pass
        reply = self._nam.get(request)
        self._reply = reply
        self.voice_status.setText("Loading voices…")
        reply.finished.connect(lambda r=reply: self._on_voices(r))

    def _cancel_fetch(self) -> None:
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.abort()

    def _on_voices(self, reply: QNetworkReply) -> None:
        if reply is not self._reply:  # cancelled or superseded
            reply.deleteLater()
            return
        self._reply = None
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                self.voice_status.setText(
                    f"Could not load voices: {reply.errorString()}"
                )
                return
            try:
                voices = extract_voice_names(
                    json.loads(reply.readAll().data())
                )
            except (ValueError, TypeError, AttributeError):
                self.voice_status.setText(
                    "Could not parse the voices response."
                )
                return
            self._set_voice_choices(
                voices, self._current_selection(), keep_missing=False
            )
            self.voice_status.setText(f"{len(voices)} voices loaded.")
        finally:
            reply.deleteLater()

    # -- example voice playback --------------------------------------------- #
    def _play_example_voice(self, index: int) -> None:
        voice_name = self.voice_boxes[index].currentData()
        if not voice_name:
            return
        self._play_example(
            f"Hello! I am {voice_name} voice provided by the kokoro API. This is how I sound.",
            voice_name,
            self._voice_play_btns[index],
        )

    def _play_mixed_voice(self) -> None:
        voice_str = self.voice_string_edit.text()
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
        parent = self.parent()
        if (
            parent
            and hasattr(parent, "_player")
            and parent._player is not None
        ):
            parent._player.set_paused(True)

        # If something is already playing (network request active OR audio
        # still playing from a completed request)
        is_playing = (
            self._is_example_playing() or self._active_play_btn is not None
        )
        if is_playing:
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
                if (
                    parent
                    and hasattr(parent, "_player")
                    and parent._player is not None
                ):
                    parent._player.set_paused(False)
                return
            # Otherwise switch to new voice
            self._active_play_btn = btn

        self._active_play_btn = btn
        self._last_active_btn = btn
        self._set_example_playing(True)

        from kokoro_editor.settings import speech_payload, speech_request

        s = self._settings
        if self._example_player is None:
            from kokoro_editor.audio import PCMPlayer

            self._example_player = PCMPlayer(s.sample_rate, s.prebuffer_ms)
            try:
                self._example_player.open()
            except Exception:
                self._set_example_playing(False)
                self._active_play_btn = None
                if (
                    parent
                    and hasattr(parent, "_player")
                    and parent._player is not None
                ):
                    parent._player.set_paused(False)
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

    # -- dialog plumbing ---------------------------------------------------- #
    def accept(self) -> None:
        # No voice is required here so other tabs can be saved while the server
        # is unreachable; Play and Render ask for a voice when none is set.
        if not self.url_edit.text().strip():
            self.tabs.setCurrentWidget(self.kokoro_tab)
            QMessageBox.warning(
                self, "Server URL required", "Enter the server URL."
            )
            return
        super().accept()

    def done(self, result: int) -> None:
        self._cancel_fetch()
        super().done(result)

    def result_settings(self) -> AppSettings:
        # replace() keeps settings this dialog doesn't edit (e.g. ssml).
        font = self._selected_font()
        return replace(
            self._settings,
            font_family=font.family(),
            font_size=font.pointSize(),
            base_url=self.url_edit.text().strip(),
            model=self.model_edit.text().strip() or "kokoro",
            voices=self._current_selection(),
            weights=self._current_weights(),
            speed=self.speed_spin.value(),
            volume_multiplier=self.volume_spin.value(),
            sample_rate=self.rate_spin.value(),
            prebuffer_ms=self.prebuffer_spin.value(),
            scrub_step_s=self.scrub_spin.value(),
            restore_session=self.restore_check.isChecked(),
        )
