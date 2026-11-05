"""Document playback: the speech request, streaming into the player, the
status line, and errors, with a fake server and no sound device."""

from __future__ import annotations

import pytest
from fakes import FakeNam
from PySide6.QtNetwork import QNetworkReply

from kokoro_editor import audio
from kokoro_editor.audio import BYTES_PER_FRAME, PCMPlayer
from kokoro_editor.characters import Character


@pytest.fixture
def player_window(window, monkeypatch):
    """The window with a fake server and a player that never opens a
    sound device; its audio callback is driven by the tests."""
    monkeypatch.setattr(audio, "sd", object())
    monkeypatch.setattr(PCMPlayer, "open", lambda self: None)
    window.settings.prebuffer_ms = 0
    window._nam = nam = FakeNam()
    window.panel.set_characters([Character("bob", "am_puck", 0.8)])
    window.text_edit.setPlainText("[voice:bob] Hello there.")
    return window, nam


def pull(player: PCMPlayer, frames: int = 100) -> None:
    """What the sound device does: ask the player for audio."""
    out = bytearray(frames * BYTES_PER_FRAME)
    player._callback(out, frames, None, None)


def test_play_sends_the_text_with_the_characters(player_window):
    window, nam = player_window
    window.play_btn.click()
    payload = nam.posts[0]
    assert payload["input"] == "[voice:bob] Hello there."
    assert payload["voice"] == "af_sky"
    assert payload["voice_aliases"] == {
        "bob": {"voice": "am_puck", "rate": 0.8}
    }
    assert payload["stream"] is True
    assert window.status_label.text().startswith("Waiting for server…")
    assert window.play_btn.text() == "Pause"


def test_streams_plays_and_finishes(player_window):
    window, nam = player_window
    window.play_btn.click()
    player, reply = window._player, nam.replies[0]
    reply.data = bytes(400)
    reply.readyRead.emit()
    pull(player)
    window._tick()
    assert window.status_label.text().startswith("Playing")
    assert window.status_label.text().endswith("(receiving)")
    reply.finished.emit()
    pull(player)
    pull(player)
    window._tick()
    assert window.status_label.text() == "Finished."
    assert window._player is None and window.play_btn.text() == "Play"


def test_pause_resume_and_stop(player_window):
    window, nam = player_window
    window.play_btn.click()
    window.play_btn.click()
    assert window._player.paused and window.play_btn.text() == "Resume"
    assert window.status_label.text().startswith("Paused")
    window.play_btn.click()
    assert window.play_btn.text() == "Pause"
    window.stop_btn.click()
    assert window._player is None
    assert window.status_label.text() == "Stopped"
    assert nam.replies[0].aborted


def test_server_error_is_reported(player_window, boxes):
    window, nam = player_window
    window.play_btn.click()
    reply = nam.replies[0]
    reply.status = 400
    reply.data = b'{"detail": "Voice \'bob\' not found"}'
    reply.readyRead.emit()
    reply.finished.emit()
    assert boxes.last == "HTTP 400: Voice 'bob' not found"
    assert window._player is None
    assert window.status_label.text() == "Request failed."


def test_lost_connection_plays_what_arrived(player_window, boxes):
    window, nam = player_window
    window.play_btn.click()
    reply = nam.replies[0]
    reply.data = bytes(400)
    reply.readyRead.emit()
    reply.status = None
    reply.network_error = QNetworkReply.NetworkError.RemoteHostClosedError
    reply.finished.emit()
    window._tick()
    assert "connection lost; playing what was received" in (
        window.status_label.text()
    )
    assert boxes.messages == []


def test_no_connection_at_all(player_window, boxes):
    window, nam = player_window
    window.play_btn.click()
    reply = nam.replies[0]
    reply.status = None
    reply.network_error = QNetworkReply.NetworkError.ConnectionRefusedError
    reply.finished.emit()
    assert boxes.last == "Connection refused"
    assert window._player is None


def test_no_audio_returned(player_window):
    window, nam = player_window
    window.play_btn.click()
    nam.replies[0].finished.emit()
    pull(window._player)
    window._tick()
    assert window.status_label.text() == "The server returned no audio."


def test_without_audio_output(player_window, boxes, monkeypatch):
    window, nam = player_window
    monkeypatch.setattr(audio, "sd", None)
    window.play_btn.click()
    assert boxes.messages[-1][1] == "Audio output unavailable"
    assert nam.posts == []
