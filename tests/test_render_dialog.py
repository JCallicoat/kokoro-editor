"""Rendering to a file, with a fake server."""

from __future__ import annotations

import pytest
from fakes import FakeNam
from PySide6.QtNetwork import QNetworkReply
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from kokoro_editor.render_dialog import RenderDialog
from kokoro_editor.settings import AppSettings

ALIASES = {"bob": {"voice": "am_puck", "rate": 0.8}}


@pytest.fixture
def render(qapp, boxes, tmp_path):
    nam = FakeNam()
    dialog = RenderDialog(
        AppSettings(voices=["af_sky", "", ""]),
        "[voice:bob] Hello.",
        str(tmp_path / "story"),
        False,
        nam,
        ALIASES,
    )
    dialog.nam = nam
    return dialog


def test_starts_with_the_documents_name_in_the_first_format(render, tmp_path):
    assert render.path_edit.text() == str(tmp_path / "story.opus")
    render.format_box.setCurrentIndex(render.format_box.findData("mp3"))
    assert render.path_edit.text() == str(tmp_path / "story.mp3")


def test_browsing_to_a_known_extension_switches_format(
    render, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: (str(tmp_path / "x.wav"), "")),
    )
    render._browse()
    assert render.format_box.currentData() == "wav"
    assert render.path_edit.text() == str(tmp_path / "x.wav")


def test_renders_with_the_characters_and_saves(render, tmp_path):
    render._render()
    payload = render.nam.posts[0]
    assert payload["voice_aliases"] == ALIASES
    assert payload["stream"] is False
    assert payload["response_format"] == "opus"
    assert render.progress.isVisibleTo(render)
    reply = render.nam.replies[0]
    reply.data = b"OggS..."
    reply.finished.emit()
    assert (tmp_path / "story.opus").read_bytes() == b"OggS..."
    assert render.saved_path == str(tmp_path / "story.opus")
    assert render.result() == QDialog.DialogCode.Accepted


def test_server_error_is_shown(render, boxes, tmp_path):
    render._render()
    reply = render.nam.replies[0]
    reply.status = 400
    reply.data = b'{"detail": "Voice \'x\' not found"}'
    reply.finished.emit()
    assert boxes.last == "HTTP 400: Voice 'x' not found"
    assert render.status_label.text() == "Rendering failed."
    assert not (tmp_path / "story.opus").exists()
    assert render.path_edit.isEnabled()


def test_network_error_and_empty_audio(render, boxes):
    render._render()
    reply = render.nam.replies[0]
    reply.status = None
    reply.network_error = QNetworkReply.NetworkError.ConnectionRefusedError
    reply.finished.emit()
    assert boxes.last == "Connection refused"
    render._render()
    render.nam.replies[1].finished.emit()
    assert render.status_label.text() == "The server returned no audio."


def test_checks_before_rendering(render, boxes, tmp_path):
    render.path_edit.setText("")
    render._render()
    assert boxes.last == "Enter a file name."
    render.path_edit.setText(str(tmp_path / "missing" / "x.opus"))
    render._render()
    assert boxes.last.endswith("does not exist.")
    existing = tmp_path / "x.opus"
    existing.write_bytes(b"old")
    render.path_edit.setText(str(existing))
    boxes.answers = [QMessageBox.StandardButton.No]
    render._render()
    assert boxes.last.endswith("already exists. Replace it?")
    assert render.nam.posts == []


def test_cancelling_abandons_the_request(render):
    render._render()
    render.reject()
    assert render.nam.replies[0].aborted
