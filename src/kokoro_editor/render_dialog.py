"""Render dialog: synthesize the text in one request and save it to a file."""

from __future__ import annotations

import json
import os

from PySide6.QtCore import QByteArray
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from kokoro_editor.settings import (
    AppSettings,
    describe_http_error,
    speech_payload,
    speech_request,
)

# Output formats supported by Kokoro-FastAPI: (response_format, label).
# Each format's file extension is the same as its name.
FORMATS = [
    ("opus", "Opus"),
    ("mp3", "MP3"),
    ("aac", "AAC"),
    ("flac", "FLAC"),
    ("wav", "WAV"),
    ("pcm", "Raw PCM (16-bit mono)"),
]
FORMAT_EXTENSIONS = {f".{fmt}" for fmt, _ in FORMATS}
# The whole file is generated before the first byte arrives, so allow a long silence.
RENDER_INACTIVITY_TIMEOUT_MS = 600_000


def with_extension(path: str, fmt: str) -> str:
    """Swap a known audio extension for fmt's, or append it if there is none."""
    root, ext = os.path.splitext(path)
    if ext.lower() not in FORMAT_EXTENSIONS:
        root = path
    return f"{root}.{fmt}"


class RenderDialog(QDialog):
    def __init__(
        self,
        settings: AppSettings,
        text: str,
        base_path: str,
        selection: bool,
        nam: QNetworkAccessManager,
        voice_aliases: dict[str, str | dict],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Render Audio")
        self.setMinimumWidth(520)
        self._settings = settings
        self._text = text
        self._voice_aliases = voice_aliases
        # Use the caller's long-lived manager: destroying a QNetworkAccessManager
        # along with this dialog right after a POST crashes Qt.
        self._nam = nam
        self._reply: QNetworkReply | None = None
        self._path = ""
        self.saved_path = ""

        self.path_edit = QLineEdit()
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_btn)

        self.format_box = QComboBox()
        for fmt, label in FORMATS:
            self.format_box.addItem(label, fmt)
        self.format_box.currentIndexChanged.connect(self._on_format_changed)

        source = "the selection" if selection else "the whole document"
        form = QFormLayout()
        form.addRow("File", path_row)
        form.addRow("Format", self.format_box)
        form.addRow("Text", QLabel(f"{source} ({len(text):,} characters)"))

        self.progress = QProgressBar()
        self.progress.setRange(
            0, 0
        )  # indeterminate: the server gives no progress
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.status_label = QLabel("")

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._render)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.progress)
        layout.addWidget(self.status_label)
        layout.addWidget(self.buttons)

        self.path_edit.setText(with_extension(base_path, self._format()))

    def _format(self) -> str:
        return self.format_box.currentData()

    def _on_format_changed(self) -> None:
        path = self.path_edit.text().strip()
        if path:
            self.path_edit.setText(with_extension(path, self._format()))

    def _browse(self) -> None:
        fmt = self._format()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Render To",
            self.path_edit.text().strip(),
            f"{self.format_box.currentText()} (*.{fmt});;All files (*)",
        )
        if not path:
            return
        # Picking a file with another known extension switches to that format.
        index = self.format_box.findData(os.path.splitext(path)[1][1:].lower())
        if index >= 0:
            self.format_box.blockSignals(True)
            self.format_box.setCurrentIndex(index)
            self.format_box.blockSignals(False)
        self.path_edit.setText(with_extension(path, self._format()))

    def _set_busy(self, busy: bool, message: str = "") -> None:
        for widget in (
            self.path_edit,
            self.browse_btn,
            self.format_box,
            self.buttons.button(QDialogButtonBox.StandardButton.Save),
        ):
            widget.setEnabled(not busy)
        self.progress.setVisible(busy)
        self.status_label.setText(message)

    # -- rendering ---------------------------------------------------------- #
    def _render(self) -> None:
        path = os.path.abspath(
            os.path.expanduser(self.path_edit.text().strip())
        )
        if not self.path_edit.text().strip():
            QMessageBox.warning(self, "File required", "Enter a file name.")
            return
        if not os.path.isdir(os.path.dirname(path)):
            QMessageBox.warning(
                self,
                "Folder not found",
                f"{os.path.dirname(path)} does not exist.",
            )
            return
        if os.path.exists(path):
            answer = QMessageBox.question(
                self, "Replace file?", f"{path} already exists. Replace it?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        payload = speech_payload(
            self._settings,
            self._text,
            self._format(),
            stream=False,
            voice_aliases=self._voice_aliases,
        )
        request = speech_request(
            self._settings.base_url, RENDER_INACTIVITY_TIMEOUT_MS
        )
        reply = self._nam.post(
            request, QByteArray(json.dumps(payload).encode("utf-8"))
        )
        reply.finished.connect(lambda r=reply: self._on_finished(r))
        self._reply = reply
        self._path = path
        self._set_busy(True, "Rendering… this can take a while for long text.")

    def _on_finished(self, reply: QNetworkReply) -> None:
        if reply is not self._reply:  # cancelled
            reply.deleteLater()
            return
        self._reply = None
        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        error = reply.error()
        error_text = reply.errorString()
        data = bytes(reply.readAll().data())
        reply.deleteLater()

        if status is not None and status >= 400:
            self._set_busy(False, "Rendering failed.")
            QMessageBox.warning(
                self, "Server error", describe_http_error(status, data)
            )
            return
        if error != QNetworkReply.NetworkError.NoError:
            self._set_busy(False, "Rendering failed.")
            QMessageBox.warning(self, "Request failed", error_text)
            return
        if not data:
            self._set_busy(False, "The server returned no audio.")
            return
        try:
            with open(self._path, "wb") as f:
                f.write(data)
        except OSError as exc:
            self._set_busy(False, "Could not save the file.")
            QMessageBox.critical(
                self, "Could not save file", f"{self._path}\n\n{exc}"
            )
            return
        self.saved_path = self._path
        self.accept()

    def done(self, result: int) -> None:
        # Cancel, Esc or closing the window while rendering abandons the request.
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.abort()
        super().done(result)
