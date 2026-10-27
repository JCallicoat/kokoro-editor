"""In-memory PCM buffer played through a sounddevice output stream."""

from __future__ import annotations

import threading
from dataclasses import dataclass

try:
    import sounddevice as sd

    SD_ERROR: str | None = None
except (ImportError, OSError) as exc:  # OSError: PortAudio library not found
    sd = None
    SD_ERROR = str(exc)

BYTES_PER_FRAME = 2  # mono, signed 16-bit little-endian


@dataclass
class PlayerState:
    position_s: float
    total_s: float
    total_bytes: int
    paused: bool
    primed: bool
    complete: bool
    finished: bool


class PCMPlayer:
    """Holds the whole stream in memory and plays it from a movable position.

    The network side calls feed()/mark_complete(); the audio thread calls
    _callback(). Everything shared is guarded by one lock, and the callback
    never touches Qt objects.
    """

    def __init__(self, sample_rate: int, prebuffer_ms: int) -> None:
        self.sample_rate = sample_rate
        self._bytes_per_second = sample_rate * BYTES_PER_FRAME
        prebuffer = int(self._bytes_per_second * prebuffer_ms / 1000)
        self._prebuffer = prebuffer - prebuffer % BYTES_PER_FRAME

        self._lock = threading.Lock()
        self._buf = bytearray()
        self._carry = b""  # odd trailing byte of a network chunk
        self._pos = 0
        self._paused = False
        self._primed = False  # True once enough audio is buffered to (re)start
        self._complete = False  # server finished sending
        self._finished = False  # playback reached the end
        self._stream = None

    # -- lifecycle ---------------------------------------------------------- #
    def open(self) -> None:
        self._stream = sd.RawOutputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            callback=self._callback,
        )
        self._stream.start()

    def close(self, drain: bool = False) -> None:
        """Stop the stream and free the buffer. drain=True lets queued audio finish."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                if drain:
                    stream.stop()
                else:
                    stream.abort()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        with self._lock:
            self._buf = bytearray()
            self._carry = b""
            self._pos = 0

    # -- network side ------------------------------------------------------- #
    def feed(self, data: bytes) -> None:
        with self._lock:
            data = self._carry + data
            usable = len(data) - len(data) % BYTES_PER_FRAME
            self._buf += data[:usable]
            self._carry = data[usable:]

    def mark_complete(self) -> None:
        with self._lock:
            self._complete = True

    # -- control / inspection ---------------------------------------------- #
    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._paused = paused

    def seek(self, seconds: float) -> None:
        """Jump to an absolute position, clamped to the audio received so far."""
        with self._lock:
            self._set_position(int(seconds * self._bytes_per_second))

    def seek_by(self, delta_seconds: float) -> None:
        """Move the position relative to where playback currently is."""
        with self._lock:
            self._set_position(
                self._pos + int(delta_seconds * self._bytes_per_second)
            )

    def _set_position(self, target: int) -> None:  # caller must hold the lock
        target -= target % BYTES_PER_FRAME
        self._pos = max(0, min(target, len(self._buf)))

    def snapshot(self) -> PlayerState:
        with self._lock:
            bps = self._bytes_per_second
            return PlayerState(
                position_s=self._pos / bps,
                total_s=len(self._buf) / bps,
                total_bytes=len(self._buf),
                paused=self._paused,
                primed=self._primed,
                complete=self._complete,
                finished=self._finished,
            )

    # -- audio thread ------------------------------------------------------- #
    def _callback(self, outdata, frames, time_info, status) -> None:
        need = frames * BYTES_PER_FRAME
        chunk = b""
        with self._lock:
            if not self._paused and not self._finished:
                avail = len(self._buf) - self._pos
                if not self._primed and (
                    avail >= self._prebuffer or (self._complete and avail > 0)
                ):
                    self._primed = True
                if self._primed:
                    if avail >= need:
                        chunk = bytes(self._buf[self._pos : self._pos + need])
                        self._pos += need
                    elif self._complete:
                        # Tail of the stream (possibly nothing): play it and finish.
                        chunk = bytes(self._buf[self._pos :])
                        self._pos = len(self._buf)
                        self._finished = True
                    else:
                        # Underrun: output silence and wait until the buffer refills.
                        self._primed = False
                elif self._complete:
                    self._finished = (
                        True  # complete, never primed, nothing received
                    )
        outdata[:] = chunk + bytes(need - len(chunk))
