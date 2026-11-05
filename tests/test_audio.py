"""PCMPlayer's buffer and playback logic, driven without a sound device by
calling its audio callback directly."""

from __future__ import annotations

from kokoro_editor.audio import BYTES_PER_FRAME, PCMPlayer

RATE = 1000  # frames per second, so 2000 bytes per second


def play(player: PCMPlayer, frames: int) -> bytes:
    out = bytearray(frames * BYTES_PER_FRAME)
    player._callback(out, frames, None, None)
    return bytes(out)


def test_feed_keeps_whole_frames_and_carries_odd_bytes():
    player = PCMPlayer(RATE, prebuffer_ms=0)
    player.feed(b"\x01\x02\x03")
    assert player.snapshot().total_bytes == 2
    player.feed(b"\x04")
    assert player.snapshot().total_bytes == 4


def test_waits_for_the_prebuffer_then_plays():
    player = PCMPlayer(RATE, prebuffer_ms=100)  # 200 bytes
    player.feed(b"\x01" * 100)
    assert play(player, 10) == bytes(20)  # silence while buffering
    assert not player.snapshot().primed
    player.feed(b"\x01" * 100)
    assert play(player, 10) == b"\x01" * 20
    assert player.snapshot().primed
    assert player.snapshot().position_s == 0.01


def test_underrun_outputs_silence_until_the_buffer_refills():
    player = PCMPlayer(RATE, prebuffer_ms=0)
    player.feed(b"\x01" * 4)
    assert play(player, 2) == b"\x01" * 4
    assert play(player, 2) == bytes(4)
    assert not player.snapshot().primed


def test_complete_stream_plays_its_tail_and_finishes():
    player = PCMPlayer(RATE, prebuffer_ms=1000)
    player.feed(b"\x01" * 6)
    player.mark_complete()
    # Complete streams start without waiting for the prebuffer.
    assert play(player, 5) == b"\x01" * 6 + bytes(4)
    assert player.snapshot().finished


def test_complete_with_nothing_received_finishes():
    player = PCMPlayer(RATE, prebuffer_ms=0)
    player.mark_complete()
    play(player, 5)
    assert player.snapshot().finished
    assert player.snapshot().total_bytes == 0


def test_pause_outputs_silence_and_keeps_position():
    player = PCMPlayer(RATE, prebuffer_ms=0)
    player.feed(b"\x01" * 20)
    player.set_paused(True)
    assert play(player, 5) == bytes(10)
    assert player.snapshot().position_s == 0
    player.set_paused(False)
    assert play(player, 5) == b"\x01" * 10


def test_seeking_is_clamped_to_received_audio_and_whole_frames():
    player = PCMPlayer(RATE, prebuffer_ms=0)
    player.feed(bytes(2000))  # one second
    player.seek(0.5005)
    assert player.snapshot().position_s == 0.5
    player.seek_by(10)
    assert player.snapshot().position_s == 1.0
    player.seek_by(-5)
    assert player.snapshot().position_s == 0


def test_reset_clears_for_new_playback():
    player = PCMPlayer(RATE, prebuffer_ms=0)
    player.feed(b"\x01\x02\x03")
    player.mark_complete()
    play(player, 5)
    player.reset()
    state = player.snapshot()
    assert (state.total_bytes, state.complete, state.finished) == (
        0,
        False,
        False,
    )
    player.feed(b"\x04")
    assert player.snapshot().total_bytes == 0  # the odd byte was dropped too
