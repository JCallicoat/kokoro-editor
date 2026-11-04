"""Voice mix picker with spoken previews, shared by Settings and the
character dialog."""

from __future__ import annotations

import json
from collections.abc import Callable

from PySide6.QtCore import QByteArray, QObject, QTimer
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QStyle,
    QWidget,
)

from kokoro_editor import audio
from kokoro_editor.audio import PCMPlayer
from kokoro_editor.settings import (
    MAX_VOICE_WEIGHT,
    REQUEST_INACTIVITY_TIMEOUT_MS,
    VOICE_SLOTS,
    AppSettings,
    build_voice_string,
    speech_payload,
    speech_request,
)

POLL_INTERVAL_MS = 200


def _set_button_icon(
    button: QPushButton, pixmap: QStyle.StandardPixmap
) -> None:
    button.setIcon(button.style().standardIcon(pixmap))


class VoicePreview(QObject):
    """Speaks a short sample in a voice, one preview at a time.

    on_active(True) is called when a preview starts and on_active(False) once
    previews stop, so the caller can pause other playback in between.
    """

    def __init__(
        self,
        settings_fn: Callable[[], AppSettings],
        nam: QNetworkAccessManager,
        on_active: Callable[[bool], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings_fn = settings_fn
        self._nam = nam
        self._on_active = on_active
        self._active = False
        self._player: PCMPlayer | None = None
        self._reply: QNetworkReply | None = None
        self._button: QPushButton | None = None
        # Notices when playback reaches the end so the button can reset.
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)

    def toggle(self, text: str, voice: str, button: QPushButton) -> None:
        """Play voice from button, or stop if button's preview is playing."""
        restart = button is not self._button
        self._halt()
        if restart and voice and self._start(text, voice):
            self._button = button
            _set_button_icon(button, QStyle.StandardPixmap.SP_MediaStop)
        else:
            self._set_active(False)

    def stop(self) -> None:
        self._halt()
        self._set_active(False)

    def _start(self, text: str, voice: str) -> bool:
        if audio.sd is None:
            return False
        s = self._settings_fn()
        player = PCMPlayer(s.sample_rate, s.prebuffer_ms)
        try:
            player.open()
        except Exception:
            return False
        self._set_active(True)
        payload = speech_payload(s, text, "pcm", stream=True, voice=voice)
        request = speech_request(s.base_url, REQUEST_INACTIVITY_TIMEOUT_MS)
        reply = self._nam.post(
            request, QByteArray(json.dumps(payload).encode("utf-8"))
        )
        reply.readyRead.connect(lambda r=reply: self._on_ready(r))
        reply.finished.connect(lambda r=reply: self._on_finished(r))
        self._player = player
        self._reply = reply
        self._timer.start()
        return True

    def _halt(self, drain: bool = False) -> None:
        """End the current preview without reporting it, ready for another."""
        self._timer.stop()
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.abort()
        player, self._player = self._player, None
        if player is not None:
            player.close(drain)
        button, self._button = self._button, None
        if button is not None:
            _set_button_icon(button, QStyle.StandardPixmap.SP_MediaPlay)

    def _set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self._on_active(active)

    def _on_ready(self, reply: QNetworkReply) -> None:
        if reply is not self._reply or self._player is None:
            return
        data = bytes(reply.readAll().data())
        if not _http_failed(reply):
            self._player.feed(data)

    def _on_finished(self, reply: QNetworkReply) -> None:
        reply.deleteLater()
        if reply is not self._reply or self._player is None:
            return  # aborted by us, or superseded
        self._reply = None
        if _http_failed(reply):
            self.stop()
            return
        # On a network error, play whatever arrived; with nothing, the player
        # finishes straight away and _poll stops the preview.
        self._player.feed(bytes(reply.readAll().data()))
        self._player.mark_complete()

    def _poll(self) -> None:
        if self._player is not None and self._player.snapshot().finished:
            self._halt(drain=True)
            self._set_active(False)


def _http_failed(reply: QNetworkReply) -> bool:
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    return status is not None and status >= 400


class VoiceMixWidget(QGroupBox):
    """VOICE_SLOTS voice and weight pickers, the resulting Kokoro voice
    string, and preview buttons for each voice and for the whole mix.

    With require_first=False every slot, including the first, offers
    "(none)", so the mix can be left empty and decided later.
    """

    def __init__(
        self,
        settings_fn: Callable[[], AppSettings],
        nam: QNetworkAccessManager,
        on_preview_active: Callable[[bool], None],
        require_first: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Voice mix", parent)
        self._preview = VoicePreview(settings_fn, nam, on_preview_active, self)
        self._names: list[str] = []
        self._require_first = require_first

        self.voice_boxes = [QComboBox() for _ in range(VOICE_SLOTS)]
        self.weight_boxes = [QComboBox() for _ in range(VOICE_SLOTS)]
        self.voice_string_edit = QLineEdit()
        self.voice_string_edit.setReadOnly(True)

        form = QFormLayout(self)
        for index, (voice_box, weight_box) in enumerate(
            zip(self.voice_boxes, self.weight_boxes)
        ):
            for value in range(1, MAX_VOICE_WEIGHT + 1):
                weight_box.addItem(str(value), value)
            weight_box.setToolTip("Mixing weight relative to the other voices")
            voice_box.currentIndexChanged.connect(self._update_voice_string)
            weight_box.currentIndexChanged.connect(self._update_voice_string)
            play_btn = self._preview_button()
            play_btn.clicked.connect(
                lambda _=False, i=index, b=play_btn: self._preview_voice(i, b)
            )
            row = QHBoxLayout()
            row.addWidget(voice_box, 1)
            row.addWidget(weight_box)
            row.addWidget(play_btn)
            required = index == 0 and require_first
            label = "required" if required else "optional"
            form.addRow(f"Voice {index + 1} ({label})", row)

        mix_btn = self._preview_button()
        mix_btn.clicked.connect(lambda _=False: self._preview_mix(mix_btn))
        row = QHBoxLayout()
        row.addWidget(self.voice_string_edit, 1)
        row.addWidget(mix_btn)
        form.addRow("Resulting voice", row)

        self._fill([""] * VOICE_SLOTS, keep_missing=False)

    def _preview_button(self) -> QPushButton:
        button = QPushButton()
        button.setFlat(True)
        button.setFixedWidth(24)
        _set_button_icon(button, QStyle.StandardPixmap.SP_MediaPlay)
        return button

    # -- selection ---------------------------------------------------------- #
    def voices(self) -> list[str]:
        return [box.currentData() or "" for box in self.voice_boxes]

    def weights(self) -> list[int]:
        return [box.currentData() or 1 for box in self.weight_boxes]

    def voice_string(self) -> str:
        return build_voice_string(self.voices(), self.weights())

    def set_mix(self, voices: list[str], weights: list[int]) -> None:
        """Select voices and weights. Voices that aren't among the choices
        (e.g. the server is unreachable) are added so the mix stays valid."""
        for box, weight in zip(self.weight_boxes, weights):
            box.setCurrentIndex(max(box.findData(weight), 0))
        self._fill(voices, keep_missing=True)

    def set_choices(self, names: list[str]) -> None:
        """Replace the available voices, keeping selections still offered."""
        self._names = list(names)
        self._fill(self.voices(), keep_missing=False)

    def _fill(self, selected: list[str], keep_missing: bool) -> None:
        for index, box in enumerate(self.voice_boxes):
            box.blockSignals(True)
            box.clear()
            if index > 0 or not self._require_first:
                box.addItem("(none)", "")
            names = list(self._names)
            wanted = selected[index]
            if wanted and wanted not in names and keep_missing:
                names.append(wanted)
            for name in names:
                box.addItem(name, name)
            box.setCurrentIndex(max(box.findData(wanted), 0))
            box.blockSignals(False)
        self._update_voice_string()

    def _update_voice_string(self) -> None:
        for voice, weight_box in zip(self.voices(), self.weight_boxes):
            weight_box.setEnabled(bool(voice))
        self.voice_string_edit.setText(self.voice_string())

    # -- previews ----------------------------------------------------------- #
    def _preview_voice(self, index: int, button: QPushButton) -> None:
        voice = self.voices()[index]
        self._preview.toggle(
            f"Hello! I am {voice} voice provided by the kokoro API. This is how I sound.",
            voice,
            button,
        )

    def _preview_mix(self, button: QPushButton) -> None:
        self._preview.toggle(
            "Hello! I am the mixed voice provided by the kokoro API. This is how I sound.",
            self.voice_string(),
            button,
        )

    def hideEvent(self, event) -> None:
        # Closing the dialog or switching away from its tab ends any preview.
        self._preview.stop()
        super().hideEvent(event)
