"""The voice mix picker and voice previews, with a fake network and audio
player."""

from __future__ import annotations

from typing import ClassVar

import pytest
from fakes import FakeNam
from PySide6.QtWidgets import QPushButton

from kokoro_editor import audio, voice_mix
from kokoro_editor.settings import AppSettings
from kokoro_editor.voice_mix import PREVIEW_ALIAS, VoiceMixWidget, VoicePreview


class FakePlayer:
    instances: ClassVar[list[FakePlayer]] = []

    def __init__(self, *_args) -> None:
        self.fed = b""
        self.complete = False
        self.finished = False
        self.closed = None  # the drain argument, once closed
        FakePlayer.instances.append(self)

    def open(self) -> None:
        pass

    def close(self, drain: bool = False) -> None:
        self.closed = drain

    def feed(self, data: bytes) -> None:
        self.fed += data

    def mark_complete(self) -> None:
        self.complete = True

    def snapshot(self):
        return type("State", (), {"finished": self.finished})()


@pytest.fixture
def preview(qapp, monkeypatch):
    FakePlayer.instances = []
    monkeypatch.setattr(voice_mix, "PCMPlayer", FakePlayer)
    monkeypatch.setattr(audio, "sd", object())
    active: list[bool] = []
    nam = FakeNam()
    player = VoicePreview(lambda: AppSettings(), nam, active.append)
    player.nam, player.active = nam, active
    return player


def test_preview_plays_and_the_same_button_stops_it(preview):
    button = QPushButton()
    preview.toggle("Hi", "af_sky", button)
    assert preview.active == [True]
    assert preview.nam.posts[0]["voice"] == "af_sky"
    assert "voice_aliases" not in preview.nam.posts[0]
    preview.toggle("Hi", "af_sky", button)
    assert preview.active == [True, False]
    assert FakePlayer.instances[0].closed is False  # stopped, not drained


def test_switching_previews_stays_active(preview):
    first, second = QPushButton(), QPushButton()
    preview.toggle("Hi", "af_sky", first)
    preview.toggle("Hi", "am_puck", second)
    assert preview.active == [True]
    assert FakePlayer.instances[0].closed is False
    assert preview._button is second


def test_preview_feeds_audio_and_stops_when_played(preview):
    preview.toggle("Hi", "af_sky", QPushButton())
    reply, player = preview.nam.replies[0], FakePlayer.instances[0]
    reply.data = b"\x01\x02"
    reply.readyRead.emit()
    reply.data = b"\x03"
    reply.finished.emit()
    assert (player.fed, player.complete) == (b"\x01\x02\x03", True)
    player.finished = True
    preview._poll()
    assert player.closed is True  # drained
    assert preview.active == [True, False]


def test_server_error_stops_the_preview(preview):
    preview.toggle("Hi", "af_sky", QPushButton())
    reply = preview.nam.replies[0]
    reply.status = 400
    reply.data = b'{"detail": "no"}'
    reply.readyRead.emit()
    reply.finished.emit()
    assert FakePlayer.instances[0].fed == b""
    assert preview.active == [True, False]


def test_rate_is_sent_through_a_throwaway_alias(preview):
    preview.toggle("Hi", "af_sky+am_puck", QPushButton(), rate=0.8)
    payload = preview.nam.posts[0]
    assert payload["voice"] == PREVIEW_ALIAS
    assert payload["voice_aliases"] == {
        PREVIEW_ALIAS: {"voice": "af_sky+am_puck", "rate": 0.8}
    }


def test_no_preview_without_audio_output(preview, monkeypatch):
    monkeypatch.setattr(audio, "sd", None)
    preview.toggle("Hi", "af_sky", QPushButton())
    assert preview.nam.posts == [] and preview.active == []


# -- the widget -------------------------------------------------------- #
@pytest.fixture
def mix(qapp):
    return VoiceMixWidget(lambda: AppSettings(), FakeNam(), lambda _a: None)


def items(box) -> list[str]:
    return [box.itemText(i) for i in range(box.count())]


def test_first_voice_required_by_default(mix):
    mix.set_choices(["af_sky", "am_puck"])
    assert items(mix.voice_boxes[0]) == ["af_sky", "am_puck"]
    assert items(mix.voice_boxes[1]) == ["(none)", "af_sky", "am_puck"]
    assert mix.voice_string() == "af_sky"


def test_first_voice_optional(qapp):
    widget = VoiceMixWidget(
        lambda: AppSettings(), FakeNam(), lambda _a: None, require_first=False
    )
    widget.set_choices(["af_sky"])
    assert items(widget.voice_boxes[0]) == ["(none)", "af_sky"]
    assert widget.voice_string() == ""


def test_set_mix_keeps_voices_missing_from_the_choices(mix):
    mix.set_mix(["af_sky", "am_puck", ""], [2, 1, 1])
    assert mix.voice_string() == "af_sky(2)+am_puck(1)"
    assert mix.voice_string_edit.text() == "af_sky(2)+am_puck(1)"
    assert not mix.weight_boxes[2].isEnabled()
    # Loading the server's list drops what it doesn't have.
    mix.set_choices(["af_sky"])
    assert mix.voices() == ["af_sky", "", ""]


def test_hiding_the_widget_stops_previews(mix, monkeypatch):
    stopped = []
    monkeypatch.setattr(mix._preview, "stop", lambda: stopped.append(True))
    mix.show()
    mix.hide()
    assert stopped == [True]
